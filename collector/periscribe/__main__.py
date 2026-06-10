"""CLI 엔트리.

  periscribe                  (더블클릭/인자 없음) GUI 설치 창(토큰 붙여넣기). exe가 아니면 run.
  periscribe setup            콘솔 대화형 설치(토큰 입력). 터미널용
  periscribe [run] [옵션]     수집 루프 실행
  periscribe install ...      비대화형 설치(--token[/--url]). 자동화/스크립트용
  periscribe uninstall        자동시작 해제

자동시작은 HKCU Run 레지스트리 키(관리자 권한 불필요)로 등록한다.

옵션은 config.json / 환경변수(PERISCRIBE_*) / 커맨드라인 순으로 덮어쓴다.
단일 exe(PyInstaller)에서도 동일하게 동작한다(sys.frozen 감지).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

from . import __version__
from .collector import Collector, _child_env
from .config import Config
from .sink import IngestSink, StdoutSink

TASK_NAME = "PeriscribeCollector"
# [제거됨] 옛 failsafe guardian 의 자동시작 키 이름. 가디언은 비활성화됐고(컬렉터·프록시 분리, 수동 관리)
# 더는 등록하지 않는다. 이 상수는 옛 버전이 남긴 등록을 청소(_del_autostart)하기 위해서만 유지한다.
GUARDIAN_TASK_NAME = "PeriscribeGuardian"

# 배포본에 내장되는 기본 ingest 엔드포인트. 사용자는 토큰만 넣으면 된다(URL 입력 불필요).
# 빌드/실행 시 PERISCRIBE_DEFAULT_INGEST_URL 로 덮어쓸 수 있다.
DEFAULT_INGEST_URL = os.environ.get(
    "PERISCRIBE_DEFAULT_INGEST_URL",
    "https://wgzsjdmohbawfcxiicqc.supabase.co/functions/v1/ingest",
)

SYSMON_DOWNLOAD_URL = "https://live.sysinternals.com/Sysmon64.exe"

# Sysmon 설정: 전체 ProcessCreate(EID1) + ProcessTerminate(EID5) 로깅(그 외 이벤트는 미로깅).
# 컬렉터가 ProcessGuid 계보로 Claude(claude.exe) 서브트리만 골라 적재 → Supabase 볼륨은 Claude 것만.
# (계보 추적엔 모든 create 가 필요 — 깊은 손자의 부모가 shell 이 아닐 수 있음.)
# onmatch="exclude" + 규칙없음 = 전부 포함 / onmatch="include" + 규칙없음 = 미로깅.
SYSMON_CONFIG_XML = """<Sysmon schemaversion="4.50">
  <EventFiltering>
    <ProcessCreate onmatch="exclude" />
    <ProcessTerminate onmatch="exclude" />
    <FileCreate onmatch="include" />
    <NetworkConnect onmatch="include" />
    <ImageLoad onmatch="include" />
    <RawAccessRead onmatch="include" />
    <DnsQuery onmatch="include" />
  </EventFiltering>
</Sysmon>
"""


def _is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


def _relaunch_elevated(argv: list[str]) -> int:
    """현재 프로세스를 UAC 승격으로 재실행(audit-setup 한정). 승격 인스턴스가 실제 작업 수행."""
    import ctypes
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, _join_args(argv)
    else:
        exe, params = sys.executable, "-m periscribe " + _join_args(argv)
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)  # type: ignore[attr-defined]
    if rc <= 32:
        print("[audit-setup] 관리자 권한 승격이 거부/실패했습니다(UAC).", file=sys.stderr)
        return 1
    print("[audit-setup] 관리자 권한 창에서 계속 진행됩니다.")
    return 0


def _join_args(argv: list[str]) -> str:
    return " ".join(f'"{a}"' if " " in a else a for a in argv)


def _cleanup_stale_mei() -> None:
    """onefile(windowed) 빌드가 종료 시 Tk DLL 잠금으로 못 지운 과거 _MEI 임시폴더를 청소.
    현재 실행 중인 _MEIPASS는 제외. (정리 실패 메시지박스/디스크 누적 방지)"""
    if not getattr(sys, "frozen", False):
        return
    import glob
    import shutil
    import tempfile
    cur = getattr(sys, "_MEIPASS", "")
    for d in glob.glob(os.path.join(tempfile.gettempdir(), "_MEI*")):
        if d == cur or not os.path.isdir(d):
            continue
        shutil.rmtree(d, ignore_errors=True)  # 잠긴(사용 중) 폴더는 조용히 건너뜀


def _exit_no_cleanup(code: int) -> None:
    """Tk를 로드한 onefile에서 정상 종료 시 발생하는 '_MEI 삭제 실패' 메시지박스를 피한다.
    부트로더의 atexit 정리를 건너뛰고 즉시 종료(임시폴더는 다음 실행의 _cleanup_stale_mei가 청소)."""
    if getattr(sys, "frozen", False):
        sys.stdout.flush() if sys.stdout else None
        sys.stderr.flush() if sys.stderr else None
        os._exit(code)


def _data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Periscribe"


def _installed_config_path() -> Path:
    return _data_dir() / "config.json"


def _is_installed() -> bool:
    """LOCALAPPDATA에 토큰이 든 config가 있으면 설치된 것으로 본다."""
    p = _installed_config_path()
    if not p.is_file():
        return False
    try:
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        return bool(d.get("device_token"))
    except Exception:
        return False


def _hide_console() -> None:
    """백그라운드(작업 스케줄러) 실행 시 콘솔 창을 숨긴다. console exe라 로그온 시 잠깐 떴다 사라짐."""
    if os.name != "nt":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def _pause() -> None:
    """더블클릭으로 뜬 콘솔이 즉시 닫혀 메시지를 못 보는 일을 막는다."""
    try:
        input("\nEnter 키를 누르면 종료합니다...")
    except Exception:
        pass


# 자동시작: 작업 스케줄러(schtasks)는 환경에 따라 권한을 타서 "액세스 거부"가 난다.
# HKCU\...\Run 은 현재 사용자 키라 관리자 권한 없이 등록되고 로그온 시 자동 실행된다.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _set_autostart(name: str, command: str) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, command)


def _del_autostart(name: str) -> None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
    except FileNotFoundError:
        pass


def _get_autostart(name: str) -> str | None:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as k:
            v, _t = winreg.QueryValueEx(k, name)
            return str(v) or None
    except OSError:
        return None


def _collector_exe() -> Path | None:
    """컬렉터/guardian 을 띄울 collector exe 경로 해석(frozen 전용 의미).
    고정 설치 위치를 두지 않으므로 '자가치유로 항상 최신 위치를 담는 HKCU Run 값'을 진실의 원천으로
    삼는다. periscribe-proxy.exe 같은 별도 exe 가 collector 를 찾을 때 이 경로로 위임/spawn 한다."""
    if getattr(sys, "frozen", False) and Path(sys.executable).stem.lower() == "periscribe":
        return Path(sys.executable)  # collector 자기 자신
    cmd = _get_autostart(TASK_NAME)  # 설치/실행 시 자가등록된 Run 값: "{exe}" run -c "{config}"
    if cmd:
        s = cmd.strip()
        exe = s[1:s.index('"', 1)] if s.startswith('"') and s.count('"') >= 2 else s.split(" ", 1)[0]
        p = Path(exe)
        # dev 설치(pythonw -m periscribe ...) 값은 거부
        if p.is_file() and p.suffix.lower() == ".exe" and "periscribe" in p.stem.lower():
            return p
    return None


def _start_collector(config_path: Path) -> None:
    """수집기를 분리된 백그라운드 프로세스(창 없음)로 즉시 실행한다."""
    if getattr(sys, "frozen", False):
        args = [str(_collector_exe() or sys.executable), "run", "-c", str(config_path)]
    else:
        pyw = str(Path(sys.executable).with_name("pythonw.exe"))
        args = [pyw, "-m", "periscribe", "run", "-c", str(config_path)]
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    try:
        subprocess.Popen(args, env=_child_env(), creationflags=flags, close_fds=True)
    except Exception:
        pass


def _start_proxy_process(config_path: Path) -> None:
    """API 프록시를 분리된 백그라운드 프로세스(창 없음)로 직접 띄운다. 컬렉터와 무관한 독립 프로세스.
    'proxy on' 이 호출한다(컬렉터 supervision/guardian 없음 → 죽으면 'proxy off' 로 수동 직결)."""
    exe = str(_collector_exe() or sys.executable)
    if getattr(sys, "frozen", False):
        args = [exe, "proxy-run", "-c", str(config_path)]
    else:
        pyw = str(Path(sys.executable).with_name("pythonw.exe"))
        args = [pyw, "-m", "periscribe", "proxy-run", "-c", str(config_path)]
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    try:
        subprocess.Popen(args, env=_child_env(), creationflags=flags, close_fds=True)
    except Exception:
        pass


def _stop_proxy_process() -> None:
    """실행 중인 API 프록시(proxy-run) 프로세스를 종료한다('proxy off'). Windows 전용."""
    if os.name != "nt":
        return
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='periscribe.exe'\" | "
          "Where-Object { $_.CommandLine -match 'proxy-run' } | "
          "ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }")
    try:
        subprocess.call(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _reconcile_autostart() -> None:
    """자가치유 자동시작: 현재 실행 중인 exe 의 위치로 HKCU Run 값을 맞춘다.
    exe 를 어디 두든/옮기든, 그 위치에서 한 번 실행되면(또는 컬렉터가 주기적으로) Run 값이 자동 동기화돼
    '고정 설치 위치' 없이도 자동시작이 깨지지 않는다. 같은 값 이름을 덮어쓰므로 옛 경로는 자동 소멸.
    frozen+Windows 전용(개발 모드/타 OS는 무의미)."""
    if not (getattr(sys, "frozen", False) and os.name == "nt"):
        return
    if Path(sys.executable).stem.lower() != "periscribe":
        return  # collector exe 만 자기 위치를 등록(proxy.exe 등이 run 을 타도 오염 방지)
    try:
        cfg = _installed_config_path()
        want = f'"{sys.executable}" run -c "{cfg}"'
        if _get_autostart(TASK_NAME) != want:
            _set_autostart(TASK_NAME, want)
        # 가디언은 제거됐다 → 옛 버전이 남긴 guardian 자동시작 등록이 있으면 청소한다.
        if _get_autostart(GUARDIAN_TASK_NAME):
            _del_autostart(GUARDIAN_TASK_NAME)
    except Exception:
        pass  # 자가치유는 보조 기능 — 실패해도 본 동작에 영향 없음


# ---------------- run ----------------
def cmd_run(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="periscribe run", description="Claude Code transcript collector")
    p.add_argument("-c", "--config", default="config.json", help="설정 파일 경로")
    p.add_argument("--watch-dir")
    p.add_argument("--machine-id")
    p.add_argument("--poll-interval", type=float)
    p.add_argument("--backfill", type=int)
    p.add_argument("--store-raw", action="store_true")
    p.add_argument("--redact", action="store_true")
    p.add_argument("--ingest-url")
    p.add_argument("--device-token")
    p.add_argument("--dry-run", action="store_true", help="적재 대신 stdout 출력(테스트)")
    a = p.parse_args(argv)

    # 단일 exe로 백그라운드 실행될 때(작업 스케줄러) 콘솔 창을 숨긴다. 진단은 log_file로.
    if getattr(sys, "frozen", False) and not a.dry_run:
        _hide_console()

    # 자가치유: 현재 exe 위치로 자동시작 값을 동기화(부팅/수동 실행 시 1회). exe를 옮겨도 깨지지 않게.
    if not a.dry_run:
        _reconcile_autostart()

    cfg = Config.load(a.config)
    if a.watch_dir: cfg.watch_dir = a.watch_dir
    if a.machine_id: cfg.machine_id = a.machine_id
    if a.poll_interval is not None: cfg.poll_interval = a.poll_interval
    if a.backfill is not None: cfg.backfill = a.backfill
    if a.store_raw: cfg.store_raw = True
    if a.redact: cfg.redact = True
    if a.ingest_url: cfg.ingest_url = a.ingest_url
    if a.device_token: cfg.device_token = a.device_token

    if a.dry_run:
        sink = StdoutSink()
    else:
        try:
            cfg.validate()
        except ValueError as e:
            print(f"[periscribe] 설정 오류: {e}", file=sys.stderr)
            return 2
        sink = IngestSink(cfg.ingest_url, cfg.device_token,
                          machine_id=cfg.machine_id, collector_version=__version__)
    Collector(cfg, sink).run()
    return 0


# ---------------- install ----------------
def cmd_install(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="periscribe install", description="이 PC에 Collector 설치(부팅 자동실행)")
    p.add_argument("--token", required=True, help="웹에서 발급받은 디바이스 토큰")
    p.add_argument("--url", default=DEFAULT_INGEST_URL,
                   help="ingest 엔드포인트(.../functions/v1/ingest). 생략 시 내장 기본값")
    p.add_argument("--name", default="", help="machine_id(비우면 hostname)")
    p.add_argument("--data-dir", default=str(_data_dir()))
    p.add_argument("--task-name", default=TASK_NAME)
    p.add_argument("--exe", default="", help="등록할 실행 경로(기본: 자동 감지)")
    p.add_argument("--dry-run", action="store_true", help="실제 등록 없이 계획만 출력")
    a = p.parse_args(argv)

    data = Path(a.data_dir)
    config_path = data / "config.json"
    cfg = {
        "watch_dir": "", "machine_id": a.name, "poll_interval": 0.4,
        # 컨테이너(devcontainer) transcript 루트. devcontainer.json이 컨테이너의
        # ~/.claude/projects 를 %USERPROFILE%\periscribe-agents\<이름> 으로 바인드하므로
        # 그 부모를 기본 감시. 폴더가 없으면 discover()가 건너뛰어 무해(컨테이너 미사용 시).
        "container_root": str(Path.home() / "periscribe-agents"),
        "ingest_url": a.url, "device_token": a.token, "batch_size": 500,
        "checkpoint_path": str(data / "checkpoints" / "offsets.json"),
        "backfill": 0, "store_raw": False, "store_thinking": False, "redact": True,
        "heartbeat_interval": 30, "log_file": str(data / "logs" / "collector.log"),
        "log_max_bytes": 5000000, "log_backups": 3,
    }
    # 등록할 명령(개별 onefile exe: 실행 중인 위치를 자동시작에 등록 → 자가치유로 위치 추종).
    if a.exe:
        run_cmd = f'"{a.exe}" run -c "{config_path}"'
    elif getattr(sys, "frozen", False):
        run_cmd = f'"{sys.executable}" run -c "{config_path}"'
    else:
        pyw = str(Path(sys.executable).with_name("pythonw.exe"))
        run_cmd = f'"{pyw}" -m periscribe run -c "{config_path}"'

    print(f"[install] config: {config_path}")
    print(f"[install] 자동시작 '{a.task_name}': {run_cmd}")
    if a.dry_run:
        print("[install] --dry-run: 실제 변경 없음")
        return 0

    data.mkdir(parents=True, exist_ok=True)
    (data / "checkpoints").mkdir(exist_ok=True)
    (data / "logs").mkdir(exist_ok=True)
    config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    # 자동 시작 등록: HKCU\...\Run (관리자 권한 불필요 → schtasks "액세스 거부" 문제 회피).
    _set_autostart(a.task_name, run_cmd)
    # 옛 버전(schtasks)으로 설치했던 흔적이 있으면 정리.
    if os.name == "nt":
        subprocess.call(["schtasks", "/Delete", "/TN", a.task_name, "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 다음 로그인까지 기다리지 않고 지금 바로 백그라운드 실행.
    _start_collector(config_path)
    print(f"[install] 완료. 헬스바에 곧 표시됩니다. 로그: {cfg['log_file']}")
    return 0


# ---------------- audit-setup (OS 레벨 쉘/프로세스 감사 켜기) ----------------
def _current_user() -> str:
    dom = os.environ.get("USERDOMAIN") or socket.gethostname()
    return f"{dom}\\{os.environ.get('USERNAME', '')}"


def _find_sysmon(data: "Path") -> str:
    import shutil
    cands = []
    if getattr(sys, "frozen", False):
        cands.append(Path(sys.executable).with_name("Sysmon64.exe"))
    cands.append(data / "Sysmon64.exe")
    w = shutil.which("Sysmon64.exe") or shutil.which("Sysmon.exe")
    if w:
        cands.append(Path(w))
    for c in cands:
        if c and Path(c).is_file():
            return str(c)
    return ""


def _download_sysmon(data: "Path") -> str:
    dest = data / "Sysmon64.exe"
    try:
        import urllib.request
        print(f"[audit-setup] Sysmon64.exe 다운로드 중… ({SYSMON_DOWNLOAD_URL})")
        urllib.request.urlretrieve(SYSMON_DOWNLOAD_URL, str(dest))
        return str(dest) if dest.is_file() else ""
    except Exception as e:  # noqa: BLE001
        print(f"[audit-setup] 다운로드 실패: {e}", file=sys.stderr)
        return ""


def _enable_os_exec_in_config(p: "Path") -> None:
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig")) if p.is_file() else {}
    except Exception:
        data = {}
    data["os_exec_enabled"] = True
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_audit_setup(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="periscribe audit-setup",
        description="OS 레벨 쉘/프로세스 실행 감사 켜기 (Sysmon 설치 + 로그읽기 권한, 관리자 1회)")
    p.add_argument("--config", default=str(_installed_config_path()))
    p.add_argument("--sysmon", default="", help="Sysmon64.exe 경로(생략 시 동봉/다운로드)")
    p.add_argument("--user", default="", help="로그 읽기 권한 부여 대상 계정(기본: 현재 사용자)")
    p.add_argument("--dry-run", action="store_true", help="실제 변경 없이 계획만 출력")
    a = p.parse_args(argv)

    if os.name != "nt":
        print("[audit-setup] Windows 전용입니다(현재는). 다른 OS는 후속(eBPF/auditd).", file=sys.stderr)
        return 2

    # 관리자 권한 필요 → 아니면 UAC 자기승격(승격 인스턴스가 실제 작업 수행).
    if not a.dry_run and not _is_admin():
        print("[audit-setup] 관리자 권한이 필요합니다 — UAC 승격을 요청합니다…")
        return _relaunch_elevated(["audit-setup"] + argv)

    data = _data_dir()
    data.mkdir(parents=True, exist_ok=True)
    sysmon_cfg = data / "periscribe-sysmon.xml"
    sysmon_cfg.write_text(SYSMON_CONFIG_XML, encoding="utf-8")
    user = a.user or _current_user()
    sysmon = a.sysmon or _find_sysmon(data)

    print(f"[audit-setup] Sysmon 설정: {sysmon_cfg}")
    print(f"[audit-setup] Sysmon 실행파일: {sysmon or '(다운로드 예정)'}")
    print(f"[audit-setup] 로그읽기 권한 대상: {user}")
    print(f"[audit-setup] config: {a.config}")
    if a.dry_run:
        print("[audit-setup] --dry-run: 실제 변경 없음")
        return 0

    if not sysmon:
        sysmon = _download_sysmon(data)
        if not sysmon:
            print("[audit-setup] Sysmon64.exe 확보 실패. --sysmon 로 경로를 지정하세요.", file=sys.stderr)
            return 3

    # Sysmon 설치(또는 이미 설치됐으면 설정만 갱신).
    rc = subprocess.call([sysmon, "-accepteula", "-i", str(sysmon_cfg)])
    if rc != 0:
        rc = subprocess.call([sysmon, "-c", str(sysmon_cfg)])
    if rc != 0:
        print(f"[audit-setup] Sysmon 설치/설정 실패(rc={rc}).", file=sys.stderr)
        return rc

    # 컬렉터(비관리자) 사용자에게 Sysmon Operational 로그 읽기 권한 부여.
    subprocess.call(["net", "localgroup", "Event Log Readers", user, "/add"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    _enable_os_exec_in_config(Path(a.config))
    print("[audit-setup] 완료 ✅  컬렉터를 재시작하면(로그아웃/로그인 또는 재실행) OS 쉘/프로세스 실행을 "
          "수집합니다(웹에서 🐚 OS).")
    print("[audit-setup] 끄려면: config 의 os_exec_enabled=false + (선택) Sysmon 제거 'Sysmon64 -u'.")
    return 0


# ---------------- Claude API 게이트웨이(로컬 프록시: 로깅 + 요청측 통제) ----------------
def _settings_json_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _proxy_policy_path() -> Path:
    return _data_dir() / "proxy-policy.json"


def _proxy_spool_path(cfg) -> Path:
    import re
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", cfg.machine_id or "host") or "host"
    return Path(cfg.watch_dir) / "_apilog" / f"{safe}.jsonl"


def _set_config_keys(p: Path, keys: dict) -> None:
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig")) if p.is_file() else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data.update(keys)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _merge_claude_env(env_updates: dict) -> None:
    from . import proxyguard
    proxyguard.merge_settings_env(env_updates)


def cmd_proxy_run(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="periscribe proxy-run")
    p.add_argument("-c", "--config", default="config.json")
    a = p.parse_args(argv)
    if getattr(sys, "frozen", False):
        _hide_console()
    from . import apiproxy, proxycert
    cfg = Config.load(a.config)
    certs = proxycert.ensure_certs(_data_dir())
    spool = _proxy_spool_path(cfg)
    policy = _proxy_policy_path()
    apiproxy.run_proxy(cfg.machine_id, cfg.api_proxy_port, str(spool), str(policy),
                       certs["server_pem"], certs["server_key"],
                       logger=lambda m: print(m, file=sys.stderr, flush=True))
    return 0


def _proxy_enable(config_path: Path, port: int = 0) -> tuple[bool, list[str]]:
    """프록시 ON(컬렉터/guardian 과 무관한 독립 프로세스). lockout-safe 순서:
    프록시 프로세스 직접 기동 → 헬스 검증 → 성공 시에만 env 주입(검증 실패 시 env 안 씀 → Claude 직결 정상).
    cmd_proxy_setup / cmd_proxy / gui_proxy 가 공유한다."""
    import time
    from . import proxycert, proxyguard, proxypolicy
    out: list[str] = []
    cfg = Config.load(str(config_path))
    port = port or cfg.api_proxy_port
    certs = proxycert.ensure_certs(_data_dir())
    base_url = f"https://127.0.0.1:{port}"

    # 1) 설정 기록 + 정책 파일 보장.
    _set_config_keys(config_path, {"api_log_enabled": True, "api_proxy_port": port})
    proxypolicy.ensure_policy_file(str(_proxy_policy_path()))

    # 2) 프록시 프로세스를 직접 띄운다(이미 serve 중이 아니면). 컬렉터와 무관한 독립 프로세스.
    if not proxyguard.port_alive(port):
        _start_proxy_process(config_path)

    # 3) 프록시가 "실제로 serve" 할 때까지 검증(최대 SETUP_WAIT_S). 헬스 프로브 성공해야만 다음으로.
    deadline = time.time() + proxyguard.SETUP_WAIT_S
    healthy = False
    while time.time() < deadline:
        if proxyguard.port_alive(port) and proxyguard.health_probe(port, certs["ca_pem"]):
            healthy = True
            break
        time.sleep(0.5)

    # 4) 검증 성공 시에만 env 기록(= Claude 라우팅 전환). 실패하면 env 안 씀 → Claude 는 계속 직결로 정상.
    if not healthy:
        out.append("⚠ 프록시가 제한시간 내 기동/응답하지 않아 env 미기록(Claude 직결로 정상).")
        out.append(f"  다시 'proxy on' 으로 재시도하세요. 진단: {cfg.log_file or 'config 의 log_file'}")
        return False, out
    # 핫리로드 적용 범위 판정: CA(NODE_EXTRA_CA_CERTS)는 Node 가 시작 시에만 읽는다. 이번 enable 전에
    # 이미 상주해 있었다면 떠 있는 세션도 base_url 핫리로드만으로 무중단 전환되지만, 최초 1회는 아니다.
    ca_was_resident = proxyguard.env_has_ca()
    saved_orig = proxyguard.route_to_proxy(base_url, certs["ca_pem"])
    if saved_orig:
        out.append(f"기존 ANTHROPIC_BASE_URL({saved_orig})을 보관 — 끄면 복원됩니다.")
        out.append("  ⚠ 켜져 있는 동안 해당 게이트웨이는 우회됩니다(프록시는 api.anthropic.com 직결 중계).")
    proxyguard.write_status({"env_present": True, "proxy_healthy": True,
                             "last_action": "enabled", "reason": "proxy enable verified",
                             "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    if ca_was_resident:
        out.append(f"완료 ✅  즉시 적용(실행 중 Claude 세션 포함) → 인풋/아웃풋/작업 로깅(웹 🛰 API). base_url={base_url}")
    else:
        out.append(f"완료 ✅  로컬 프록시 경유 → 인풋/아웃풋/작업 로깅(웹 🛰 API). base_url={base_url}")
        out.append("  ⚠ 지금 떠 있는 Claude 세션은 이번 1회만 재시작 필요(신뢰 CA가 세션 시작 시에만 로드됨). 이후 켜기/끄기는 무중단.")
    out.append(f"통제(차단/레닥션/주입): {_proxy_policy_path()} 편집.")
    out.append("  ⚠ 가디언 없음(수동 관리): 프록시가 죽으면 자동 직결복구가 없습니다 → 'proxy off' 로 직결 전환하세요.")
    return True, out


def _proxy_disable(config_path: Path) -> tuple[bool, list[str]]:
    """프록시 OFF. env 직결 덮어쓰기(즉시 직결) → 프록시 프로세스 종료 → 설정 off."""
    import time
    from . import proxyguard
    proxyguard.strip_proxy_env()       # BASE_URL 을 직결로 덮어씀(즉시 직결). 상주 CA 는 유지 → 다음 ON 무중단
    _stop_proxy_process()              # 독립 프록시 프로세스 종료
    _set_config_keys(config_path, {"api_log_enabled": False})
    _del_autostart(GUARDIAN_TASK_NAME)                            # 옛 guardian 자동시작 잔재 청소
    proxyguard.write_status({"env_present": False, "proxy_healthy": False,
                             "last_action": "teardown", "reason": "manual disable",
                             "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    return True, ["완료. 실행 중인 Claude 세션 포함 즉시 Anthropic 직결로 전환됩니다(프록시 종료·로깅 중지)."]


def _proxy_status(config_path: Path) -> dict:
    """현재 프록시 상태 판정(가디언 없음). state:
    'on'(env=우리프록시+헬스OK) / 'degraded'(env=우리프록시지만 비정상 → 수동 off 필요) / 'off'(직결)."""
    from . import proxyguard
    cfg = Config.load(str(config_path))
    port = cfg.api_proxy_port
    env_present = proxyguard.env_has_proxy()
    alive = proxyguard.port_alive(port)
    healthy = False
    if alive:
        try:
            from . import proxycert
            healthy = proxyguard.health_probe(port, proxycert.ensure_certs(_data_dir())["ca_pem"])
        except Exception:
            healthy = False
    intent = bool(getattr(cfg, "api_log_enabled", False))
    if env_present and healthy:
        state = "on"
    elif env_present:
        state = "degraded"   # env 가 죽은/비정상 프록시를 가리킴 → 자동복구 없음, 'proxy off' 로 직결 전환
    else:
        state = "off"
    st = proxyguard.read_status()
    return {"state": state, "port": port, "env_present": env_present, "port_alive": alive,
            "healthy": healthy, "intent": intent, "base_url": f"https://127.0.0.1:{port}",
            "last_action": st.get("last_action")}


def cmd_proxy_setup(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="periscribe proxy-setup",
        description="Claude API 게이트웨이 켜기(인/아웃/작업 로깅 + 요청측 통제). Claude를 로컬 프록시로 경유(무관리자).")
    p.add_argument("--config", default=str(_installed_config_path()))
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    from . import proxycert, proxyguard
    cfg = Config.load(a.config)
    port = a.port or cfg.api_proxy_port
    print(f"[proxy-setup] settings: {_settings_json_path()}")
    if a.dry_run:
        certs = proxycert.ensure_certs(_data_dir())
        print(f"[proxy-setup] CA: {certs['ca_pem']}")
        print(f"[proxy-setup] ANTHROPIC_BASE_URL = https://127.0.0.1:{port}")
        print("[proxy-setup] --dry-run: 변경 없음")
        return 0
    print(f"[proxy-setup] 프록시 기동/검증 중… (최대 {int(proxyguard.SETUP_WAIT_S)}s)")
    ok, lines = _proxy_enable(Path(a.config), port)
    for ln in lines:
        print(f"[proxy-setup] {ln}")
    if ok:
        print("[proxy-setup] 끄기: periscribe.exe proxy-teardown (또는 proxy off)")
    return 0 if ok else 3


def cmd_proxy_teardown(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="periscribe proxy-teardown")
    p.add_argument("--config", default=str(_installed_config_path()))
    a = p.parse_args(argv)
    _ok, lines = _proxy_disable(Path(a.config))
    for ln in lines:
        print(f"[proxy-teardown] {ln}")
    return 0


def _proxy_status_text(s: dict) -> tuple[str, str]:
    """status dict → (짧은 라벨, 상세 설명). CLI/GUI 공유."""
    if s["state"] == "on":
        return "🟢 켜짐", f"{s['base_url']} 경유로 로깅 중입니다."
    if s["state"] == "off":
        return "⚪ 꺼짐", "Anthropic 에 직접 연결됩니다(로깅 안 함)."
    # degraded — env 가 우리 프록시를 가리키는데 비정상(가디언 없음 → 수동 조치 필요).
    return ("🔴 프록시 비정상", "settings 의 프록시 주소가 응답하지 않습니다. 자동복구가 없으니 "
            "'끄기'(proxy off)로 직결 전환하세요.")


def cmd_proxy(argv: list[str]) -> int:
    """Claude API 프록시 on/off 토글(한 명령으로 켜고/끄기). settings.json env 의 ANTHROPIC_BASE_URL 을
    우리 프록시/직결 URL 로 안전하게 전환한다(키 삭제는 실행 중 세션에 미반영이라 항상 덮어쓰기)."""
    p = argparse.ArgumentParser(
        prog="periscribe proxy",
        description="Claude API 프록시(로깅+통제) 켜기/끄기. on|off|toggle|status.")
    p.add_argument("action", nargs="?", default="status",
                   choices=["on", "off", "toggle", "status"])
    p.add_argument("--config", default=str(_installed_config_path()))
    p.add_argument("--port", type=int, default=0)
    a = p.parse_args(argv)
    cfgpath = Path(a.config)

    if a.action == "status":
        s = _proxy_status(cfgpath)
        label, detail = _proxy_status_text(s)
        print(f"[proxy] 상태: {label} — {detail}")
        print(f"[proxy] base_url={s['base_url']}  env_present={s['env_present']}  "
              f"port_alive={s['port_alive']}  healthy={s['healthy']}  intent={s['intent']}")
        return 0

    action = a.action
    if action == "toggle":
        action = "off" if _proxy_status(cfgpath)["state"] == "on" else "on"
        print(f"[proxy] 토글 → {action}")

    if action == "on":
        ok, lines = _proxy_enable(cfgpath, a.port)
    else:
        ok, lines = _proxy_disable(cfgpath)
    for ln in lines:
        print(f"[proxy] {ln}")
    return 0 if ok else 3


def _create_proxy_shortcut() -> None:
    """바탕화면에 'Periscribe 프록시' 바로가기(collector exe proxy-gui) 1회 생성(frozen·Windows 전용).
    타깃은 collector exe(_collector_exe) — proxy.exe 를 지워도 바로가기가 동작하도록."""
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return
    try:
        col = _collector_exe()
        exe = str(col) if col else sys.executable
        desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
        lnk = desktop / "Periscribe 프록시.lnk"
        if lnk.exists():
            return
        ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}');"
              f"$s.TargetPath='{exe}';$s.Arguments='proxy-gui';$s.IconLocation='{exe},0';"
              f"$s.Description='Claude API 프록시 켜기/끄기';$s.Save()")
        subprocess.call(["powershell", "-NoProfile", "-Command", ps],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def cmd_guardian_run(argv: list[str]) -> int:
    """[제거됨] 옛 failsafe guardian. 컬렉터↔프록시 분리 + 가디언 비활성화로 더는 동작하지 않는다.
    옛 버전이 남긴 guardian 자동시작이 이 명령을 타더라도 즉시 자기 등록만 청소하고 종료한다."""
    # 인자 파싱은 호환 위해 흡수만 한다.
    argparse.ArgumentParser(prog="periscribe guardian-run").parse_known_args(argv)
    if getattr(sys, "frozen", False):
        _hide_console()
    try:
        _del_autostart(GUARDIAN_TASK_NAME)   # 옛 guardian 자동시작 잔재 자가 청소
    except Exception:
        pass
    print("[guardian] 비활성화됨(컬렉터·프록시 분리, 수동 관리) → 종료", file=sys.stderr, flush=True)
    return 0


# ---------------- setup (대화형) ----------------
def cmd_setup(argv: list[str]) -> int:
    """더블클릭/인자 없음 진입점. 토큰만 입력받아 자동 설치한다(URL은 내장 기본값)."""
    print("=" * 50)
    print("  Periscribe Collector 설치")
    print("=" * 50)
    print("웹의 [⚙ 머신 관리]에서 발급한 '디바이스 토큰'을 붙여넣으세요.")
    print("(붙여넣기: 마우스 오른쪽 클릭 또는 Ctrl+V)\n")
    try:
        token = input("디바이스 토큰: ").strip()
    except EOFError:
        token = ""
    if not token:
        print("\n토큰이 비어 있어 설치를 중단합니다.")
        _pause()
        return 2

    default_name = socket.gethostname()
    try:
        name = input(f"머신 이름 [{default_name}]: ").strip()
    except EOFError:
        name = ""

    print("\n설치 중...")
    install_args = ["--token", token, "--url", DEFAULT_INGEST_URL]
    if name:
        install_args += ["--name", name]
    rc = cmd_install(install_args)
    if rc == 0:
        print("\n✓ 설치 완료! 잠시 후 웹 헬스바에 이 PC가 표시됩니다.")
        print("  백그라운드에서 자동 실행되며, 로그온할 때마다 자동 시작됩니다.")
        print("  이 창은 닫아도 됩니다.  (제거: periscribe.exe uninstall)")
    else:
        print(f"\n✗ 설치 실패 (코드 {rc}). 위 메시지를 확인하세요.")
    _pause()
    return rc


# ---------------- uninstall ----------------
def cmd_uninstall(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="periscribe uninstall")
    p.add_argument("--task-name", default=TASK_NAME)
    a = p.parse_args(argv)
    _del_autostart(a.task_name)
    _del_autostart(GUARDIAN_TASK_NAME)  # API 프록시 failsafe guardian 자동시작도 함께 해제
    # 프록시 env 정리(상주 CA 제거 + ANTHROPIC_BASE_URL 은 직결값으로 덮어씀). 키를 아예 지우면
    # 실행 중 세션이 죽은 프록시 값을 영구 유지(병합 env 는 키 삭제 미반영)하므로 직결값을 남긴다.
    from . import proxyguard
    proxyguard.strip_proxy_env(include_ca=True)
    print("[uninstall] settings.json 의 ANTHROPIC_BASE_URL 은 직결 기본값으로 남겨둠(실행 중 세션 보호).")
    print("            모든 Claude 세션 종료 후에는 지워도 됩니다.")
    if os.name == "nt":
        # 옛 schtasks 설치 흔적도 제거(있으면).
        subprocess.call(["schtasks", "/End", "/TN", a.task_name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.call(["schtasks", "/Delete", "/TN", a.task_name, "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _stop_proxy_process()  # 실행 중인 API 프록시도 함께 종료(직결 복귀)
    print("[uninstall] 자동시작 해제됨. 실행 중인 수집기는 다음 로그인부터 시작되지 않습니다.")
    return 0


# ---------------- GUI 설치(더블클릭) ----------------
def gui_setup() -> int:
    """모던 설치 창(customtkinter). 미번들이면 기본 tk 창으로, 그것도 없으면 콘솔로 폴백."""
    try:
        import customtkinter as ctk
    except Exception:
        return _gui_setup_tk()

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    ACCENT, ACCENT_H, INK = "#6ea8fe", "#5a93e6", "#0b0d11"
    MUTED, OKC, ERRC = "#8a93a6", "#4cd585", "#ff6b6b"

    app = ctk.CTk()
    app.title("Periscribe Collector")
    app.resizable(False, False)
    app.configure(fg_color="#0f1115")

    card = ctk.CTkFrame(app, corner_radius=16, fg_color="#171a21")
    card.pack(padx=18, pady=18, fill="both", expand=True)
    pad = {"padx": 26}

    ctk.CTkLabel(card, text="⌖  Periscribe Collector",
                 font=ctk.CTkFont(size=19, weight="bold")).pack(anchor="w", pady=(22, 2), **pad)
    ctk.CTkLabel(card, text="에이전트 활동을 기록합니다", text_color=MUTED,
                 font=ctk.CTkFont(size=12)).pack(anchor="w", pady=(0, 16), **pad)

    ctk.CTkLabel(card, text="디바이스 토큰", text_color=MUTED,
                 font=ctk.CTkFont(size=12)).pack(anchor="w", **pad)
    token_var = ctk.StringVar()
    token_entry = ctk.CTkEntry(card, textvariable=token_var, width=360, height=38,
                               corner_radius=8, placeholder_text="웹 [머신 관리]에서 발급한 pscb_…")
    token_entry.pack(pady=(4, 12), **pad)
    token_entry.focus_set()

    ctk.CTkLabel(card, text="머신 이름 (선택)", text_color=MUTED,
                 font=ctk.CTkFont(size=12)).pack(anchor="w", **pad)
    name_var = ctk.StringVar()
    ctk.CTkEntry(card, textvariable=name_var, width=360, height=38, corner_radius=8,
                 placeholder_text=socket.gethostname()).pack(pady=(4, 6), **pad)

    status = ctk.CTkLabel(card, text="", text_color=ERRC, font=ctk.CTkFont(size=12),
                          wraplength=360, justify="left")
    status.pack(anchor="w", pady=(4, 0), **pad)
    if _is_installed():
        status.configure(text="이미 설치돼 있습니다. 새 토큰으로 다시 설치할 수 있습니다.", text_color=MUTED)

    def do_install() -> None:
        token = token_var.get().strip()
        if not token:
            status.configure(text="디바이스 토큰을 입력하세요.", text_color=ERRC)
            return
        name = name_var.get().strip()
        btn.configure(state="disabled", text="설치 중…")
        status.configure(text="", text_color=MUTED)
        app.update()
        try:
            args = ["--token", token] + (["--name", name] if name else [])
            rc = cmd_install(args)
        except Exception as e:
            rc = 1
            status.configure(text=f"오류: {e}", text_color=ERRC)
        if rc == 0:
            status.configure(text="✓ 설치 완료! 백그라운드에서 실행 중입니다. 잠시 후 웹에 표시됩니다.",
                             text_color=OKC)
            btn.configure(text="완료", fg_color=OKC, hover_color=OKC, command=app.destroy, state="normal")
            app.after(2800, app.destroy)
        else:
            if not status.cget("text"):
                status.configure(text=f"설치 실패 (코드 {rc}).", text_color=ERRC)
            btn.configure(state="normal", text="설치")

    btn = ctk.CTkButton(card, text="설치", height=42, corner_radius=10, command=do_install,
                        font=ctk.CTkFont(size=14, weight="bold"),
                        fg_color=ACCENT, hover_color=ACCENT_H, text_color=INK)
    btn.pack(fill="x", pady=(14, 22), **pad)
    app.bind("<Return>", lambda _e: do_install())

    app.update_idletasks()
    w, h = app.winfo_width(), app.winfo_height()
    x = (app.winfo_screenwidth() - w) // 2
    y = (app.winfo_screenheight() - h) // 3
    app.geometry(f"+{x}+{y}")
    app.mainloop()
    _exit_no_cleanup(0)  # Tk 로드 onefile의 _MEI 정리 실패 팝업 회피
    return 0


def _gui_setup_tk() -> int:
    """기본 tkinter 폴백 창(customtkinter 미번들 시)."""
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception:
        return cmd_setup([])

    root = tk.Tk()
    root.title("Periscribe Collector 설치")
    root.resizable(False, False)
    frm = tk.Frame(root, padx=22, pady=18)
    frm.pack(fill="both", expand=True)

    tk.Label(frm, text="Periscribe Collector 설치", font=("Segoe UI", 13, "bold")).pack(anchor="w")
    tk.Label(frm, text="웹 [⚙ 머신 관리]에서 발급받은 디바이스 토큰을 붙여넣으세요.",
             fg="#555").pack(anchor="w", pady=(2, 12))

    tk.Label(frm, text="디바이스 토큰").pack(anchor="w")
    token_var = tk.StringVar()
    ent = tk.Entry(frm, textvariable=token_var, width=54)
    ent.pack(fill="x")
    ent.focus_set()

    tk.Label(frm, text="머신 이름 (선택)").pack(anchor="w", pady=(10, 0))
    name_var = tk.StringVar(value=socket.gethostname())
    tk.Entry(frm, textvariable=name_var, width=54).pack(fill="x")

    status = tk.Label(frm, text="", fg="#c0392b", wraplength=360, justify="left")
    status.pack(anchor="w", pady=(10, 0))

    if _is_installed():
        status.config(text="이미 설치돼 있습니다. 새 토큰으로 다시 설치할 수 있습니다.", fg="#555")

    def do_install() -> None:
        token = token_var.get().strip()
        if not token:
            status.config(text="디바이스 토큰을 입력하세요.", fg="#c0392b")
            return
        name = name_var.get().strip()
        btn.config(state="disabled", text="설치 중…")
        root.update()
        try:
            args = ["--token", token]
            if name:
                args += ["--name", name]
            rc = cmd_install(args)
            if rc == 0:
                messagebox.showinfo(
                    "Periscribe",
                    "설치 완료!\n\n백그라운드에서 자동 실행되며,\n로그인할 때마다 자동으로 시작됩니다.\n웹 화면에 잠시 후 이 PC가 표시됩니다.",
                )
                root.destroy()
            else:
                status.config(text=f"설치 실패 (코드 {rc}).", fg="#c0392b")
                btn.config(state="normal", text="설치")
        except Exception as e:
            status.config(text=f"오류: {e}", fg="#c0392b")
            btn.config(state="normal", text="설치")

    btn = tk.Button(frm, text="설치", command=do_install, width=14)
    btn.pack(anchor="e", pady=(14, 0))
    root.bind("<Return>", lambda _e: do_install())

    # 화면 중앙 배치
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    x = (root.winfo_screenwidth() - w) // 2
    y = (root.winfo_screenheight() - h) // 3
    root.geometry(f"+{x}+{y}")

    root.mainloop()
    _exit_no_cleanup(0)  # Tk 로드 onefile의 _MEI 정리 실패 팝업 회피
    return 0


# ---------------- GUI 프록시 토글(더블클릭 런처) ----------------
def gui_proxy() -> int:
    """프록시 ON/OFF 토글 창(customtkinter). 현재 상태 표시 + 한 버튼 토글.
    customtkinter 미번들이면 콘솔 CLI(periscribe proxy)로 폴백."""
    # 런처화: periscribe-proxy.exe(별도 배포 exe)는 자기 위치를 시스템에 박지 않고, 실제 처리를
    # 설치된 collector exe 에 위임한다(버전 정합 + cross-exe spawn 1회로 _MEI 안전, 이후는 same-exe).
    if getattr(sys, "frozen", False) and Path(sys.executable).stem.lower() != "periscribe":
        col = _collector_exe()
        if col:
            _create_proxy_shortcut()
            try:
                subprocess.Popen([str(col), "proxy-gui"], env=_child_env(), close_fds=True)
            except Exception:
                pass
            return 0
        # collector 미설치 → 아래 미설치 안내 창으로 진행(위임 불가)
    _create_proxy_shortcut()
    try:
        import customtkinter as ctk
    except Exception:
        print("[proxy-gui] GUI(customtkinter) 미설치 → CLI 사용: periscribe proxy on|off|status")
        return cmd_proxy(["status"])

    import threading
    cfgpath = _installed_config_path()
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    ACCENT, ACCENT_H, INK = "#6ea8fe", "#5a93e6", "#0b0d11"
    MUTED, OKC, WARN, ERRC = "#8a93a6", "#4cd585", "#ffcc66", "#ff6b6b"

    # collector exe 면 config+token 으로, proxy.exe(런처)면 collector exe 해석 실패로 미설치 판정.
    # (proxy.exe 가 여기 왔다는 건 위임 실패 = _collector_exe() None = 설치 안 됐거나 Run 깨짐)
    _not_installed = not _is_installed() or (
        getattr(sys, "frozen", False)
        and Path(sys.executable).stem.lower() != "periscribe"
        and _collector_exe() is None
    )
    if _not_installed:
        # 컬렉터 미설치: 토글 UI 대신 안내 창(설정 로드가 불가능해 켜기 자체가 성립 안 함)
        app = ctk.CTk()
        app.title("Periscribe 프록시")
        app.resizable(False, False)
        app.configure(fg_color="#0f1115")
        card = ctk.CTkFrame(app, corner_radius=16, fg_color="#171a21")
        card.pack(padx=18, pady=18, fill="both", expand=True)
        pad = {"padx": 26}
        ctk.CTkLabel(card, text="🛰  Claude API 프록시",
                     font=ctk.CTkFont(size=19, weight="bold")).pack(anchor="w", pady=(22, 2), **pad)
        ctk.CTkLabel(card, text="컬렉터가 설치되어 있지 않습니다.", text_color=ERRC,
                     font=ctk.CTkFont(size=14, weight="bold")).pack(anchor="w", pady=(10, 2), **pad)
        ctk.CTkLabel(card, text="먼저 periscribe.exe 를 실행해 설치한 뒤 다시 열어주세요.\n"
                                "(다운로드: 웹 대시보드 → 머신 관리)",
                     text_color=MUTED, font=ctk.CTkFont(size=12),
                     wraplength=360, justify="left").pack(anchor="w", pady=(0, 14), **pad)
        ctk.CTkButton(card, text="닫기", height=40, corner_radius=10,
                      font=ctk.CTkFont(size=14, weight="bold"),
                      fg_color=ACCENT, hover_color=ACCENT_H, text_color=INK,
                      command=app.destroy).pack(fill="x", pady=(4, 22), **pad)
        app.update_idletasks()
        x = (app.winfo_screenwidth() - app.winfo_width()) // 2
        y = (app.winfo_screenheight() - app.winfo_height()) // 3
        app.geometry(f"+{x}+{y}")
        app.mainloop()
        _exit_no_cleanup(0)  # Tk 로드 onefile의 _MEI 정리 실패 팝업 회피
        return 0

    app = ctk.CTk()
    app.title("Periscribe 프록시")
    app.resizable(False, False)
    app.configure(fg_color="#0f1115")
    card = ctk.CTkFrame(app, corner_radius=16, fg_color="#171a21")
    card.pack(padx=18, pady=18, fill="both", expand=True)
    pad = {"padx": 26}

    ctk.CTkLabel(card, text="🛰  Claude API 프록시",
                 font=ctk.CTkFont(size=19, weight="bold")).pack(anchor="w", pady=(22, 2), **pad)
    ctk.CTkLabel(card, text="Claude ↔ Anthropic 트래픽 로깅 + 통제", text_color=MUTED,
                 font=ctk.CTkFont(size=12)).pack(anchor="w", pady=(0, 16), **pad)

    state_lbl = ctk.CTkLabel(card, text="상태 확인 중…", font=ctk.CTkFont(size=15, weight="bold"))
    state_lbl.pack(anchor="w", **pad)
    detail_lbl = ctk.CTkLabel(card, text="", text_color=MUTED, font=ctk.CTkFont(size=11),
                              wraplength=360, justify="left")
    detail_lbl.pack(anchor="w", pady=(2, 14), **pad)

    btn = ctk.CTkButton(card, text="…", height=44, corner_radius=10,
                        font=ctk.CTkFont(size=15, weight="bold"),
                        fg_color=ACCENT, hover_color=ACCENT_H, text_color=INK)
    btn.pack(fill="x", pady=(4, 14), **pad)

    msg_lbl = ctk.CTkLabel(card, text="", text_color=MUTED, font=ctk.CTkFont(size=11),
                           wraplength=360, justify="left")
    msg_lbl.pack(anchor="w", pady=(0, 18), **pad)

    ui = {"busy": False}

    def refresh() -> None:
        try:
            s = _proxy_status(cfgpath)
        except Exception as e:  # noqa: BLE001 — config 손상 등: 창은 유지하고 상태만 표기
            state_lbl.configure(text="⚠ 상태 확인 실패", text_color=ERRC)
            detail_lbl.configure(text=str(e))
            btn.configure(text="프록시 켜기(재시도)", fg_color=ACCENT, hover_color=ACCENT_H,
                          text_color=INK, command=lambda: act("on"))
            return
        label, detail = _proxy_status_text(s)
        color = {"on": OKC, "off": MUTED, "degraded": WARN}.get(s["state"], MUTED)
        if s["state"] == "degraded" and s["env_present"] and not s["healthy"]:
            color = ERRC
        state_lbl.configure(text=label, text_color=color)
        detail_lbl.configure(text=detail)
        if s["state"] == "on":
            btn.configure(text="프록시 끄기", fg_color=ERRC, hover_color="#e25b5b",
                          text_color="#ffffff", command=lambda: act("off"))
        else:
            btn.configure(text=("프록시 켜기(재시도)" if s["state"] == "degraded" else "프록시 켜기"),
                          fg_color=ACCENT, hover_color=ACCENT_H, text_color=INK,
                          command=lambda: act("on"))

    def act(action: str) -> None:
        if ui["busy"]:
            return
        ui["busy"] = True
        btn.configure(state="disabled", text="처리 중…")
        msg_lbl.configure(text=("프록시 기동/검증 중… (최대 15초)" if action == "on" else "프록시 끄는 중…"),
                          text_color=MUTED)
        app.update()

        def work() -> None:
            try:
                ok, lines = (_proxy_enable(cfgpath, 0) if action == "on" else _proxy_disable(cfgpath))
            except Exception as e:  # noqa: BLE001
                ok, lines = False, [f"오류: {e}"]

            def done() -> None:
                ui["busy"] = False
                btn.configure(state="normal")
                msg_lbl.configure(text="  ".join(lines), text_color=(OKC if ok else ERRC))
                refresh()
            app.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    refresh()
    app.update_idletasks()
    w, h = app.winfo_width(), app.winfo_height()
    x = (app.winfo_screenwidth() - w) // 2
    y = (app.winfo_screenheight() - h) // 3
    app.geometry(f"+{x}+{y}")
    app.mainloop()
    _exit_no_cleanup(0)  # Tk 로드 onefile의 _MEI 정리 실패 팝업 회피
    return 0


def main(argv: list[str] | None = None) -> int:
    # windowed(콘솔 없는) 빌드에선 stdout/stderr가 None일 수 있어 print()가 죽는다. 더미로 대체.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    # 한글 콘솔(cp949)/파일 리다이렉트에서 한글·이모지(✅ 등) print 가 UnicodeEncodeError 로 죽지 않게.
    for _name in ("stdout", "stderr"):
        try:
            getattr(sys, _name).reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    _cleanup_stale_mei()  # 묵은 _MEI 임시폴더 청소(있으면)

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "install":
        return cmd_install(argv[1:])
    if argv and argv[0] == "uninstall":
        return cmd_uninstall(argv[1:])
    if argv and argv[0] == "setup":
        return cmd_setup(argv[1:])
    if argv and argv[0] == "run":
        return cmd_run(argv[1:])
    if argv and argv[0] == "audit-setup":
        return cmd_audit_setup(argv[1:])
    if argv and argv[0] == "proxy-run":
        return cmd_proxy_run(argv[1:])
    if argv and argv[0] == "proxy-setup":
        return cmd_proxy_setup(argv[1:])
    if argv and argv[0] == "proxy-teardown":
        return cmd_proxy_teardown(argv[1:])
    if argv and argv[0] == "proxy":
        return cmd_proxy(argv[1:])
    if argv and argv[0] == "proxy-gui":
        return gui_proxy()
    if argv and argv[0] == "guardian-run":
        return cmd_guardian_run(argv[1:])

    if not argv:
        # 단일 exe 더블클릭: GUI 설치 창(설치돼 있으면 재설치 안내도 GUI에서).
        if getattr(sys, "frozen", False):
            return gui_setup()
        # 소스 실행(개발): 기존처럼 로컬 config.json 으로 run.
        return cmd_run([])

    # 알 수 없는 첫 인자 → run 옵션으로 취급(기존 호환: `periscribe --dry-run` 등).
    return cmd_run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
