"""CLI 엔트리 — 사용자 대면은 GUI 가 기본, CLI 는 커맨드 단어만(옵션 없음).

  periscribe                  (더블클릭/인자 없음) 설치됨→컨트롤 패널(트레이), 아니면 설치 창. exe 아니면 run.
  periscribe setup            콘솔 대화형 설치(토큰 입력). 터미널용
  periscribe run              수집 루프 실행(설정은 config.json) — 헤드리스 백그라운드
  periscribe panel            트레이 컨트롤 패널(상태+프록시 라우팅 토글). --tray 면 트레이 최소화로 시작
  periscribe uninstall        자동시작 해제
  periscribe proxy on|off|toggle|status   프록시 라우팅(머신 settings.json env 토글)
  periscribe audit-setup      OS 쉘/프로세스 감사 켜기(관리자 1회)

프록시 '서버' 본체는 별도 프로그램 periscribe-proxy.exe(독립 실행). 여기선 라우팅만 건다.
자동시작은 HKCU Run 레지스트리 키(관리자 권한 불필요)로 등록한다.
모든 설정은 config.json 이 담당한다(설치본: %LOCALAPPDATA%\\Periscribe\\config.json).
개발/테스트용 숨김 옵션(-c, --dry-run, --sysmon)은 --help 에 노출하지 않는다.
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


def _default_config_path() -> str:
    """-c 미지정 시 config 자동 해석: 설치본(frozen)은 LOCALAPPDATA 설치 config, 소스 실행은 ./config.json.
    (자동시작 레지스트리는 호환을 위해 계속 `run -c "<path>"` 명시 포맷으로 등록한다 — _reconcile_autostart)"""
    return str(_installed_config_path()) if getattr(sys, "frozen", False) else "config.json"


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


_SINGLE_INST_HANDLE = None


def _acquire_single_instance() -> bool:
    """수집기 단일 인스턴스 보장. 이미 떠 있으면 False(이 인스턴스 종료). Windows 네임드 뮤텍스(세션 한정).
    dev/비Windows 는 가드 없이 True. 가드 실패 시도 True(중복 위험 < 미수집 위험)."""
    global _SINGLE_INST_HANDLE
    if os.name != "nt":
        return True
    try:
        import ctypes
        h = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\PeriscribeCollector")
        if ctypes.windll.kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
            return False
        _SINGLE_INST_HANDLE = h                            # 핸들 유지(프로세스 생존 동안 뮤텍스 보유)
        return True
    except Exception:
        return True


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


# [분리] 프록시 '서버'는 독립 프로그램(periscribe-proxy.exe)이다 — 컬렉터가 spawn/kill 하지 않는다.
# 컬렉터는 라우팅(settings.json env)만 건다(_proxy_enable/_proxy_disable). 서버 생명주기는 사용자가 관리.


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
    p = argparse.ArgumentParser(prog="periscribe run",
                                description="수집 루프 실행(모든 설정은 config.json 이 담당)")
    # 숨김(개발/테스트 + 자동시작 `run -c "<path>"` 호환): 사용자 대면 옵션은 없다.
    p.add_argument("-c", "--config", default=_default_config_path(), help=argparse.SUPPRESS)
    p.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)  # 적재 대신 stdout(테스트)
    a = p.parse_args(argv)

    # 단일 exe로 백그라운드 실행될 때(작업 스케줄러) 콘솔 창을 숨긴다. 진단은 log_file로.
    if getattr(sys, "frozen", False) and not a.dry_run:
        _hide_console()

    # 자가치유: 현재 exe 위치로 자동시작 값을 동기화(부팅/수동 실행 시 1회). exe를 옮겨도 깨지지 않게.
    if not a.dry_run:
        _reconcile_autostart()
        # 단일 인스턴스: 이미 수집기가 떠 있으면 조용히 종료(패널/자동시작/설치가 각각 띄워도 1개만).
        if not _acquire_single_instance():
            print("[periscribe] 이미 수집기가 실행 중 — 중복 인스턴스 종료.")
            return 0

    cfg = Config.load(a.config)

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
def install(token: str, name: str = "", url: str = DEFAULT_INGEST_URL, *,
            data_dir: str | Path | None = None, task_name: str = TASK_NAME,
            exe: str = "", dry_run: bool = False) -> int:
    """이 PC에 Collector 설치(부팅 자동실행). CLI 표면 없음 — setup(콘솔)·GUI 설치 창이 직접 호출한다.
    token: 웹에서 발급받은 디바이스 토큰 / name: machine_id(비우면 hostname) / url: ingest 엔드포인트."""
    data = Path(data_dir) if data_dir else _data_dir()
    config_path = data / "config.json"
    cfg = {
        "watch_dir": "", "machine_id": name, "poll_interval": 0.4,
        # 컨테이너(devcontainer) transcript 루트. devcontainer.json이 컨테이너의
        # ~/.claude/projects 를 %USERPROFILE%\periscribe-agents\<이름> 으로 바인드하므로
        # 그 부모를 기본 감시. 폴더가 없으면 discover()가 건너뛰어 무해(컨테이너 미사용 시).
        "container_root": str(Path.home() / "periscribe-agents"),
        "ingest_url": url, "device_token": token, "batch_size": 500,
        "checkpoint_path": str(data / "checkpoints" / "offsets.json"),
        "backfill": 0, "store_raw": False, "store_thinking": False, "redact": True,
        "heartbeat_interval": 30, "log_file": str(data / "logs" / "collector.log"),
        "log_max_bytes": 5000000, "log_backups": 3,
    }
    # 등록할 명령(개별 onefile exe: 실행 중인 위치를 자동시작에 등록 → 자가치유로 위치 추종).
    if exe:
        run_cmd = f'"{exe}" run -c "{config_path}"'
    elif getattr(sys, "frozen", False):
        run_cmd = f'"{sys.executable}" run -c "{config_path}"'
    else:
        pyw = str(Path(sys.executable).with_name("pythonw.exe"))
        run_cmd = f'"{pyw}" -m periscribe run -c "{config_path}"'

    print(f"[install] config: {config_path}")
    print(f"[install] 자동시작 '{task_name}': {run_cmd}")
    if dry_run:
        print("[install] dry-run: 실제 변경 없음")
        return 0

    data.mkdir(parents=True, exist_ok=True)
    (data / "checkpoints").mkdir(exist_ok=True)
    (data / "logs").mkdir(exist_ok=True)
    config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    # 자동 시작 등록: HKCU\...\Run (관리자 권한 불필요 → schtasks "액세스 거부" 문제 회피).
    _set_autostart(task_name, run_cmd)
    # 옛 버전(schtasks)으로 설치했던 흔적이 있으면 정리.
    if os.name == "nt":
        subprocess.call(["schtasks", "/Delete", "/TN", task_name, "/F"],
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
    # 숨김(예외상황/개발용): --sysmon 은 자동 다운로드 실패 시 수동 경로 지정용.
    p.add_argument("--sysmon", default="", help=argparse.SUPPRESS)
    p.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
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
    user = _current_user()
    sysmon = a.sysmon or _find_sysmon(data)
    cfg_path = _installed_config_path()

    print(f"[audit-setup] Sysmon 설정: {sysmon_cfg}")
    print(f"[audit-setup] Sysmon 실행파일: {sysmon or '(다운로드 예정)'}")
    print(f"[audit-setup] 로그읽기 권한 대상: {user}")
    print(f"[audit-setup] config: {cfg_path}")
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

    _enable_os_exec_in_config(cfg_path)
    print("[audit-setup] 완료 ✅  컬렉터를 재시작하면(로그아웃/로그인 또는 재실행) OS 쉘/프로세스 실행을 "
          "수집합니다(웹에서 🐚 OS).")
    print("[audit-setup] 끄려면: config 의 os_exec_enabled=false + (선택) Sysmon 제거 'Sysmon64 -u'.")
    return 0


# ---------------- Claude API 프록시 라우팅(머신 일: settings.json env on/off) ----------------
# 프록시 '서버' 본체는 독립 프로그램(periscribe-proxy.exe). 여기선 이 머신의 Claude 를 그 서버로
# 향하게/직결로 되돌리는 **라우팅**만 한다(CA 생성·서버 실행은 서버 프로그램의 몫).
def _settings_json_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def _ca_pem_path() -> Path:
    """프록시 서버(periscribe-proxy.exe)가 생성·공유하는 CA. 컬렉터는 경로만 참조(crypto 미사용)."""
    return _data_dir() / "ca.pem"


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


def _proxy_base_url(cfg) -> str:
    host = getattr(cfg, "api_proxy_host", "127.0.0.1") or "127.0.0.1"
    return f"https://{host}:{cfg.api_proxy_port}"


def _proxy_enable(config_path: Path, port: int = 0) -> tuple[bool, list[str]]:
    """프록시 라우팅 ON(머신 일). 서버 생명주기엔 관여 안 함 — 독립 periscribe-proxy.exe 가 떠 있어야 함.
    lockout-safe: 서버가 실제 가동 중(헬스 OK)일 때만 라우팅을 건다. 아니면 직결 유지(거부).
    cmd_proxy(on) / 트레이 패널 / 테스트가 공유한다."""
    import time
    from . import proxyguard
    out: list[str] = []
    cfg = Config.load(str(config_path))
    if port:
        cfg.api_proxy_port = port
    port = cfg.api_proxy_port
    ca_pem = _ca_pem_path()
    base_url = _proxy_base_url(cfg)

    # 의도 기록(로깅 원함). 정책 파일/CA 생성은 프록시 서버의 몫.
    _set_config_keys(config_path, {"api_log_enabled": True, "api_proxy_port": port})

    # 서버가 한 번도 안 떴으면 CA 가 없다 → 라우팅 불가(서버 먼저 실행해야 함).
    if not ca_pem.is_file():
        out.append("⚠ 프록시 서버가 아직 실행된 적이 없습니다(CA 없음).")
        out.append("  periscribe-proxy.exe 를 먼저 실행한 뒤 다시 켜세요(직결 유지).")
        return False, out

    # 서버 가동 검증(죽은 프록시로 라우팅하면 Claude lockout). 성공해야만 env 라우팅.
    healthy = proxyguard.port_alive(port) and proxyguard.health_probe(port, str(ca_pem))
    if not healthy:
        out.append("⚠ 프록시 서버가 응답하지 않습니다(미실행/다른 포트).")
        out.append("  periscribe-proxy.exe 를 실행한 뒤 다시 켜세요(직결 유지).")
        return False, out

    ca_was_resident = proxyguard.env_has_ca()
    saved_orig = proxyguard.route_to_proxy(base_url, str(ca_pem))
    if saved_orig:
        out.append(f"기존 ANTHROPIC_BASE_URL({saved_orig})을 보관 — 끄면 복원됩니다.")
    proxyguard.write_status({"env_present": True, "proxy_healthy": True,
                             "last_action": "routing on", "reason": "server verified",
                             "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    if ca_was_resident:
        out.append(f"완료 ✅  즉시 적용(실행 중 Claude 세션 포함) → 프록시 경유(웹 🛰 API). base_url={base_url}")
    else:
        out.append(f"완료 ✅  프록시 경유(웹 🛰 API). base_url={base_url}")
        out.append("  ⚠ 지금 떠 있는 Claude 세션은 이번 1회만 재시작 필요(신뢰 CA가 세션 시작 시에만 로드됨). 이후 토글은 무중단.")
    out.append("  ⚠ 프록시 서버가 죽으면 자동복구 없음 → '끄기'로 직결 전환하세요.")
    return True, out


def _proxy_disable(config_path: Path) -> tuple[bool, list[str]]:
    """프록시 라우팅 OFF. env 를 직결로 덮어쓰기(즉시 직결). 프록시 서버는 독립이라 건드리지 않음."""
    import time
    from . import proxyguard
    proxyguard.strip_proxy_env()       # BASE_URL 을 직결로 덮어씀(즉시 직결). 상주 CA 는 유지 → 다음 ON 무중단
    _set_config_keys(config_path, {"api_log_enabled": False})
    proxyguard.write_status({"env_present": False, "proxy_healthy": False,
                             "last_action": "routing off", "reason": "manual disable",
                             "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    return True, ["완료. 실행 중인 Claude 세션 포함 즉시 Anthropic 직결로 전환됩니다(라우팅만 끔, 서버는 그대로)."]


def _proxy_status(config_path: Path) -> dict:
    """라우팅 상태 판정. 'on'(라우팅+서버 헬스OK) / 'degraded'(라우팅 중인데 서버 비정상) / 'off'(직결)."""
    from . import proxyguard
    cfg = Config.load(str(config_path))
    port = cfg.api_proxy_port
    ca_pem = _ca_pem_path()
    env_present = proxyguard.env_has_proxy()
    alive = proxyguard.port_alive(port)
    healthy = proxyguard.health_probe(port, str(ca_pem)) if (alive and ca_pem.is_file()) else False
    intent = bool(getattr(cfg, "api_log_enabled", False))
    if env_present and healthy:
        state = "on"
    elif env_present:
        state = "degraded"   # 라우팅 중인데 서버가 죽음 → 끄기로 직결 전환 필요
    else:
        state = "off"
    st = proxyguard.read_status()
    return {"state": state, "port": port, "env_present": env_present, "port_alive": alive,
            "healthy": healthy, "intent": intent, "base_url": _proxy_base_url(cfg),
            "last_action": st.get("last_action")}


def _proxy_status_text(s: dict) -> tuple[str, str]:
    """status dict → (짧은 라벨, 상세 설명). CLI/GUI 공유."""
    if s["state"] == "on":
        return "🟢 켜짐", f"{s['base_url']} 경유로 로깅 중입니다."
    if s["state"] == "off":
        return "⚪ 꺼짐", "Anthropic 에 직접 연결됩니다(로깅 안 함)."
    # degraded — 라우팅 중인데 서버가 응답 안 함.
    return ("🔴 서버 비정상", "프록시 서버가 응답하지 않습니다. periscribe-proxy.exe 실행 확인 후 "
            "'끄기'로 직결 전환하세요.")


def cmd_proxy(argv: list[str]) -> int:
    """Claude API 프록시 on/off 토글(한 명령으로 켜고/끄기). settings.json env 의 ANTHROPIC_BASE_URL 을
    우리 프록시/직결 URL 로 안전하게 전환한다(키 삭제는 실행 중 세션에 미반영이라 항상 덮어쓰기)."""
    p = argparse.ArgumentParser(
        prog="periscribe proxy",
        description="Claude API 프록시(로깅+통제) 켜기/끄기. on|off|toggle|status.")
    p.add_argument("action", nargs="?", default="status",
                   choices=["on", "off", "toggle", "status"])
    a = p.parse_args(argv)
    cfgpath = _installed_config_path()  # 설정은 설치 config 가 담당(포트 포함: api_proxy_port)

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
        # 미설치 가드(패널과 정합). off/status 는 잔재 청소·확인 용도로 미설치에서도 허용.
        if not _is_installed():
            print("[proxy] 컬렉터가 설치되어 있지 않습니다. periscribe.exe 를 실행해 먼저 설치하세요.",
                  file=sys.stderr)
            return 2
        ok, lines = _proxy_enable(cfgpath)
    else:
        ok, lines = _proxy_disable(cfgpath)
    for ln in lines:
        print(f"[proxy] {ln}")
    return 0 if ok else 3


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
    rc = install(token, name)
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
    del argv  # 옵션 없음(자동시작 키 이름은 TASK_NAME 고정)
    _del_autostart(TASK_NAME)
    _del_autostart(GUARDIAN_TASK_NAME)  # API 프록시 failsafe guardian 자동시작도 함께 해제
    # 프록시 env 정리(상주 CA 제거 + ANTHROPIC_BASE_URL 은 직결값으로 덮어씀). 키를 아예 지우면
    # 실행 중 세션이 죽은 프록시 값을 영구 유지(병합 env 는 키 삭제 미반영)하므로 직결값을 남긴다.
    from . import proxyguard
    proxyguard.strip_proxy_env(include_ca=True)
    print("[uninstall] settings.json 의 ANTHROPIC_BASE_URL 은 직결 기본값으로 남겨둠(실행 중 세션 보호).")
    print("            모든 Claude 세션 종료 후에는 지워도 됩니다.")
    if os.name == "nt":
        # 옛 schtasks 설치 흔적도 제거(있으면).
        subprocess.call(["schtasks", "/End", "/TN", TASK_NAME],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.call(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # 프록시 서버(periscribe-proxy.exe)는 독립 프로그램 — 사용자가 직접 닫는다(여기선 라우팅만 직결 복구).
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

    def _open_panel_and_close() -> None:
        # 설치 후 닫지 않고 컨트롤 패널로 전환(트레이 상주). 패널을 새 프로세스로 띄우고 설치창은 종료.
        try:
            if getattr(sys, "frozen", False):
                subprocess.Popen([sys.executable, "panel"], env=_child_env(), close_fds=True)
        except Exception:
            pass
        app.destroy()
        _exit_no_cleanup(0)

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
            rc = install(token, name)
        except Exception as e:
            rc = 1
            status.configure(text=f"오류: {e}", text_color=ERRC)
        if rc == 0:
            status.configure(text="✓ 설치 완료! 컨트롤 패널을 엽니다…", text_color=OKC)
            btn.configure(text="완료", fg_color=OKC, hover_color=OKC, command=_open_panel_and_close,
                          state="normal")
            app.after(1500, _open_panel_and_close)
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
            rc = install(token, name)
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
def _ensure_ca_resident() -> None:
    """프록시 서버가 만든 ca.pem 이 있으면 settings.json 에 NODE_EXTRA_CA_CERTS 를 미리 상주시킨다
    (라우팅은 건드리지 않음 = 프록시 OFF 유지). → 이후 시작된 Claude 세션은 'proxy on' 이 무중단."""
    try:
        from . import proxyguard
        ca = _ca_pem_path()
        if ca.is_file() and not proxyguard.env_has_ca():
            proxyguard.merge_settings_env({"NODE_EXTRA_CA_CERTS": str(ca)})
    except Exception:
        pass


def gui_panel() -> int:
    """컬렉터 컨트롤 패널(트레이 상주). 수집 상태 + 프록시 라우팅 ON/OFF 토글.
    창 닫기 → 시스템 트레이로 최소화(종료 아님). 수집은 데몬 스레드로 계속. 트레이에서 '종료'해야 끝.
    수집 자체는 자동시작된 헤드리스 'run' 프로세스가 담당(이중 수집 방지) — 패널은 상태/토글/트레이만.
    미설치면 설치 창으로, GUI 미가용이면 헤드리스 수집(run)으로 폴백."""
    if not _is_installed():
        return gui_setup()
    try:
        import customtkinter as ctk
    except Exception:
        print("[panel] GUI(customtkinter) 미가용 → 헤드리스 수집으로 실행")
        return cmd_run([])

    import threading
    cfgpath = _installed_config_path()
    _ensure_ca_resident()                                   # 무중단 ON 준비(라우팅 변경 없음)
    _start_collector(cfgpath)                               # 헤드리스 수집 보장(이미 떠 있으면 무해한 중복 기동)

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    ACCENT, ACCENT_H, INK = "#6ea8fe", "#5a93e6", "#0b0d11"
    MUTED, OKC, WARN, ERRC = "#8a93a6", "#4cd585", "#ffcc66", "#ff6b6b"

    cfg0 = Config.load(str(cfgpath))
    app = ctk.CTk()
    app.title("Periscribe")
    app.resizable(False, False)
    app.configure(fg_color="#0f1115")
    card = ctk.CTkFrame(app, corner_radius=16, fg_color="#171a21")
    card.pack(padx=18, pady=18, fill="both", expand=True)
    pad = {"padx": 26}

    ctk.CTkLabel(card, text="⌖  Periscribe",
                 font=ctk.CTkFont(size=19, weight="bold")).pack(anchor="w", pady=(22, 2), **pad)
    ctk.CTkLabel(card, text=f"머신 {cfg0.machine_id} · 수집 중(백그라운드)", text_color=MUTED,
                 font=ctk.CTkFont(size=12)).pack(anchor="w", pady=(0, 16), **pad)

    ctk.CTkLabel(card, text="🛰  Claude API 프록시 (라우팅)", text_color=MUTED,
                 font=ctk.CTkFont(size=12)).pack(anchor="w", **pad)
    state_lbl = ctk.CTkLabel(card, text="상태 확인 중…", font=ctk.CTkFont(size=15, weight="bold"))
    state_lbl.pack(anchor="w", pady=(2, 0), **pad)
    detail_lbl = ctk.CTkLabel(card, text="", text_color=MUTED, font=ctk.CTkFont(size=11),
                              wraplength=360, justify="left")
    detail_lbl.pack(anchor="w", pady=(2, 12), **pad)

    btn = ctk.CTkButton(card, text="…", height=44, corner_radius=10,
                        font=ctk.CTkFont(size=15, weight="bold"),
                        fg_color=ACCENT, hover_color=ACCENT_H, text_color=INK)
    btn.pack(fill="x", pady=(4, 12), **pad)

    msg_lbl = ctk.CTkLabel(card, text="", text_color=MUTED, font=ctk.CTkFont(size=11),
                           wraplength=360, justify="left")
    msg_lbl.pack(anchor="w", pady=(0, 16), **pad)

    ui = {"busy": False, "tray": None}

    def refresh() -> None:
        try:
            s = _proxy_status(cfgpath)
        except Exception as e:  # noqa: BLE001
            state_lbl.configure(text="⚠ 상태 확인 실패", text_color=ERRC)
            detail_lbl.configure(text=str(e))
            btn.configure(text="프록시 켜기(재시도)", fg_color=ACCENT, hover_color=ACCENT_H,
                          text_color=INK, command=lambda: act("on"))
            return
        label, detail = _proxy_status_text(s)
        color = {"on": OKC, "off": MUTED, "degraded": WARN}.get(s["state"], MUTED)
        if s["state"] == "degraded":
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
        msg_lbl.configure(text=("프록시 검증 중…" if action == "on" else "직결 전환 중…"), text_color=MUTED)
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

    # ----- 트레이(닫기→트레이, 종료는 트레이 메뉴) -----
    def _make_tray():
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception:
            return None
        img = Image.new("RGB", (64, 64), "#0f1115")
        d = ImageDraw.Draw(img)
        d.ellipse((16, 16, 48, 48), fill="#6ea8fe")
        def _open(icon, item):
            app.after(0, _show)
        def _quit(icon, item):
            app.after(0, _real_quit)
        return pystray.Icon("periscribe", img, "Periscribe",
                            menu=pystray.Menu(pystray.MenuItem("열기", _open),
                                              pystray.MenuItem("종료", _quit)))

    def _show() -> None:
        app.deiconify()
        app.lift()
        refresh()

    def _to_tray() -> None:
        if ui["tray"] is None:
            ui["tray"] = _make_tray()
            if ui["tray"] is not None:
                threading.Thread(target=ui["tray"].run, daemon=True).start()
        if ui["tray"] is not None:
            app.withdraw()                                  # 트레이로 숨김
        else:
            app.iconify()                                   # pystray 없으면 작업표시줄로 최소화(폴백)

    def _real_quit() -> None:
        try:
            if ui["tray"] is not None:
                ui["tray"].stop()
        except Exception:
            pass
        app.destroy()
        _exit_no_cleanup(0)

    app.protocol("WM_DELETE_WINDOW", _to_tray)              # X 버튼 → 트레이

    refresh()
    app.update_idletasks()
    w, h = app.winfo_width(), app.winfo_height()
    x = (app.winfo_screenwidth() - w) // 2
    y = (app.winfo_screenheight() - h) // 3
    app.geometry(f"+{x}+{y}")
    if "--tray" in sys.argv[1:]:                            # 자동시작 기동: 트레이 최소화로 시작
        app.after(200, _to_tray)
    app.mainloop()
    _exit_no_cleanup(0)
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
    if argv and argv[0] == "uninstall":
        return cmd_uninstall(argv[1:])
    if argv and argv[0] == "setup":
        return cmd_setup(argv[1:])
    if argv and argv[0] == "run":
        return cmd_run(argv[1:])
    if argv and argv[0] == "audit-setup":
        return cmd_audit_setup(argv[1:])
    if argv and argv[0] == "panel":
        return gui_panel()                  # 트레이 컨트롤 패널(--tray 면 트레이 최소화로 시작)
    if argv and argv[0] == "proxy":
        return cmd_proxy(argv[1:])          # 헤드리스 라우팅 토글(on|off|toggle|status)
    if argv and argv[0] == "guardian-run":
        return cmd_guardian_run(argv[1:])

    if not argv:
        # 단일 exe 더블클릭: 설치돼 있으면 트레이 컨트롤 패널, 아니면 설치 창.
        if getattr(sys, "frozen", False):
            return gui_panel() if _is_installed() else gui_setup()
        # 소스 실행(개발): 기존처럼 로컬 config.json 으로 run.
        return cmd_run([])

    # 제거된 커맨드 → 대체 경로 안내(구 문서/스크립트 호환 UX).
    removed = {
        "install": "설치는 periscribe.exe 더블클릭(GUI) 또는 'periscribe setup'(콘솔)을 사용하세요.",
        "proxy-setup": "'periscribe proxy on' 으로 통합됐습니다.",
        "proxy-teardown": "'periscribe proxy off' 로 통합됐습니다.",
        "proxy-run": "프록시 서버는 periscribe-proxy.exe 로 분리됐습니다(독립 실행).",
        "proxy-gui": "프록시 토글은 periscribe.exe 컨트롤 패널로 이동했습니다(머신에서 라우팅).",
    }
    if argv[0] in removed:
        print(f"[periscribe] '{argv[0]}' 명령은 제거됐습니다. {removed[argv[0]]}", file=sys.stderr)
        return 2

    # 대시로 시작하는 첫 인자 → run 옵션으로 취급(기존 호환: `periscribe --dry-run` 등).
    if argv[0].startswith("-"):
        return cmd_run(argv)
    print(f"[periscribe] 알 수 없는 명령: {argv[0]}\n"
          f"사용 가능: setup, run, panel, uninstall, proxy on|off|toggle|status, audit-setup",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
