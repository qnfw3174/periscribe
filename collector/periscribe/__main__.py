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
from .collector import Collector
from .config import Config
from .sink import IngestSink, StdoutSink

TASK_NAME = "PeriscribeCollector"
# API 프록시 failsafe guardian(컬렉터와 독립된 자동시작). 컬렉터가 죽어도 env 자동 제거가 동작해야 하므로
# 별도 HKCU Run 키로 등록한다. proxy-setup이 등록, proxy-teardown/uninstall이 해제.
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


def _start_collector(config_path: Path) -> None:
    """수집기를 분리된 백그라운드 프로세스(창 없음)로 즉시 실행한다."""
    if getattr(sys, "frozen", False):
        args = [sys.executable, "run", "-c", str(config_path)]
    else:
        pyw = str(Path(sys.executable).with_name("pythonw.exe"))
        args = [pyw, "-m", "periscribe", "run", "-c", str(config_path)]
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    try:
        subprocess.Popen(args, creationflags=flags, close_fds=True)
    except Exception:
        pass


def _guardian_command(config_path: Path) -> str:
    """guardian 자동시작 등록용 명령 문자열(HKCU Run)."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" guardian-run -c "{config_path}"'
    pyw = str(Path(sys.executable).with_name("pythonw.exe"))
    return f'"{pyw}" -m periscribe guardian-run -c "{config_path}"'


def _start_guardian(config_path: Path) -> None:
    """failsafe guardian 을 분리된 백그라운드 프로세스(창 없음)로 즉시 실행한다."""
    if getattr(sys, "frozen", False):
        args = [sys.executable, "guardian-run", "-c", str(config_path)]
    else:
        pyw = str(Path(sys.executable).with_name("pythonw.exe"))
        args = [pyw, "-m", "periscribe", "guardian-run", "-c", str(config_path)]
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    try:
        subprocess.Popen(args, creationflags=flags, close_fds=True)
    except Exception:
        pass


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
    # 등록할 명령
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


def cmd_proxy_setup(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="periscribe proxy-setup",
        description="Claude API 게이트웨이 켜기(인/아웃/작업 로깅 + 요청측 통제). Claude를 로컬 프록시로 경유(무관리자).")
    p.add_argument("--config", default=str(_installed_config_path()))
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    import time
    from . import proxycert, proxyguard, proxypolicy
    cfg = Config.load(a.config)
    port = a.port or cfg.api_proxy_port
    certs = proxycert.ensure_certs(_data_dir())
    base_url = f"https://127.0.0.1:{port}"
    print(f"[proxy-setup] CA: {certs['ca_pem']}")
    print(f"[proxy-setup] ANTHROPIC_BASE_URL = {base_url}")
    print(f"[proxy-setup] settings: {_settings_json_path()}")
    if a.dry_run:
        print("[proxy-setup] --dry-run: 변경 없음")
        return 0

    # 1) 의도부터 기록(env 보다 먼저). 컬렉터/guardian 이 이 값을 보고 프록시를 살린다.
    _set_config_keys(Path(a.config), {"api_log_enabled": True, "api_proxy_port": port})
    proxypolicy.ensure_policy_file(str(_proxy_policy_path()))

    # 2) 프록시를 띄울 컬렉터가 도는지 보장(프록시는 컬렉터가 supervised subprocess 로 띄움).
    if not proxyguard.port_alive(port):
        _start_collector(Path(a.config))

    # 3) env 를 쓰기 "전에" failsafe guardian 먼저 무장(등록+기동). 이후 프록시가 죽으면 guardian 이
    #    env 를 자동 제거해 Claude 를 직결로 돌린다 → lockout 불가.
    _set_autostart(GUARDIAN_TASK_NAME, _guardian_command(Path(a.config)))
    _start_guardian(Path(a.config))

    # 4) 프록시가 "실제로 serve" 할 때까지 검증(최대 SETUP_WAIT_S). 헬스 프로브 성공해야만 다음으로.
    print(f"[proxy-setup] 프록시 기동/검증 중… (최대 {int(proxyguard.SETUP_WAIT_S)}s)")
    deadline = time.time() + proxyguard.SETUP_WAIT_S
    healthy = False
    while time.time() < deadline:
        if proxyguard.port_alive(port) and proxyguard.health_probe(port, certs["ca_pem"]):
            healthy = True
            break
        time.sleep(0.5)

    # 5) 검증 성공 시에만 env 기록(= Claude 라우팅 전환). 실패하면 env 안 씀 → Claude 는 계속 직결로 정상.
    if not healthy:
        print("[proxy-setup] ⚠ 프록시가 제한시간 내 기동/응답하지 않아 env 를 쓰지 않았습니다(Claude 는 직결로 정상).",
              file=sys.stderr)
        print(f"[proxy-setup]   - 진단 로그: {cfg.log_file or '(config 의 log_file)'}")
        print("[proxy-setup]   - api_log_enabled 는 켜둔 상태라, 프록시가 나중에 뜨면 guardian 이 자동으로 켭니다.")
        print("[proxy-setup]   - 다시 시도: periscribe.exe proxy-setup")
        return 3
    proxyguard.merge_settings_env({"ANTHROPIC_BASE_URL": base_url, "NODE_EXTRA_CA_CERTS": certs["ca_pem"]})
    proxyguard.write_status({"env_present": True, "proxy_healthy": True,
                             "last_action": "readded", "reason": "proxy-setup verified",
                             "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    print("[proxy-setup] 완료 ✅  Claude를 재시작하면 로컬 프록시 경유 → 인풋/아웃풋/작업이 로깅됩니다(웹 🛰 API).")
    print(f"[proxy-setup] 통제(차단/레닥션/주입): {_proxy_policy_path()} 편집.")
    print("[proxy-setup] 보호: 프록시가 죽으면 자동으로 직결 복구(로깅만 멈춤) 후, 살아나면 자동 재개됩니다.")
    print("[proxy-setup] 끄기: periscribe.exe proxy-teardown (Claude 재시작 시 직결 복구)")
    return 0


def cmd_proxy_teardown(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="periscribe proxy-teardown")
    p.add_argument("--config", default=str(_installed_config_path()))
    a = p.parse_args(argv)
    from . import proxyguard
    proxyguard.strip_proxy_env()                                  # settings.json env 에서 프록시 키 제거
    _set_config_keys(Path(a.config), {"api_log_enabled": False})  # 의도 off → 실행 중 guardian 도 재투입 안 함
    _del_autostart(GUARDIAN_TASK_NAME)                            # guardian 자동시작 해제
    proxyguard.write_status({"env_present": False, "proxy_healthy": False,
                             "last_action": "teardown", "reason": "manual teardown",
                             "at": __import__("time").strftime("%Y-%m-%d %H:%M:%S")})
    print("[proxy-teardown] 완료. Claude 재시작 시 Anthropic 에 직접 연결됩니다.")
    return 0


def cmd_guardian_run(argv: list[str]) -> int:
    """failsafe guardian 루프. 컬렉터와 독립 프로세스로 상시 실행:
      1) 컬렉터가 멈춘 듯하면 재기동(컬렉터는 자체 watchdog 이 없음),
      2) 프록시 헬스를 보고 settings.json env 를 자동으로 빼고(직결 fail-open)/다시 넣는다(로깅 재개).
    api_log_enabled 가 false 가 되면(=teardown) 종료한다."""
    p = argparse.ArgumentParser(prog="periscribe guardian-run")
    p.add_argument("-c", "--config", default=str(_installed_config_path()))
    a = p.parse_args(argv)
    if getattr(sys, "frozen", False):
        _hide_console()
    import time
    from . import proxycert, proxyguard

    def log(m: str) -> None:
        print(m, file=sys.stderr, flush=True)

    certs = proxycert.ensure_certs(_data_dir())
    ca_pem = certs["ca_pem"]
    log("[guardian] 시작 — API 프록시 failsafe 감시")
    down_since: float | None = None
    up_since: float | None = None
    last_collector_spawn = 0.0

    while True:
        cfg = Config.load(a.config)
        if not bool(getattr(cfg, "api_log_enabled", False)):
            log("[guardian] api_log_enabled=false → 종료")
            return 0
        port = cfg.api_proxy_port
        base_url = f"https://127.0.0.1:{port}"
        now = time.time()

        # 1) 컬렉터 watchdog(휴리스틱: log_file mtime 이 오래 정지 → 재기동). 프록시는 컬렉터가 살린다.
        if _collector_stale(cfg) and now - last_collector_spawn > 30.0:
            last_collector_spawn = now
            _start_collector(Path(a.config))
            log("[guardian] 컬렉터가 멈춘 듯 → 재기동")

        # 2) 프록시 헬스 + 히스테리시스 타이머
        healthy = proxyguard.port_alive(port) and proxyguard.health_probe(port, ca_pem)
        if healthy:
            up_since = up_since or now
            down_since = None
        else:
            down_since = down_since or now
            up_since = None

        # 3) failsafe: 오래 죽었으면 env 제거(직결), 오래 살았는데 env 없으면 재투입(로깅 재개)
        env_present = proxyguard.env_has_proxy()
        if env_present and not healthy and down_since and (now - down_since) > proxyguard.DOWN_GRACE_S:
            proxyguard.strip_proxy_env()
            proxyguard.write_status({"env_present": False, "proxy_healthy": False,
                                     "last_action": "stripped", "reason": "proxy down > grace",
                                     "at": time.strftime("%Y-%m-%d %H:%M:%S")})
            log("[guardian] 프록시 비정상 지속 → env 제거(Claude 직결 fail-open). 로깅 일시중지.")
        elif (not env_present) and healthy and up_since and (now - up_since) > proxyguard.UP_STABLE_S:
            proxyguard.merge_settings_env({"ANTHROPIC_BASE_URL": base_url, "NODE_EXTRA_CA_CERTS": ca_pem})
            proxyguard.write_status({"env_present": True, "proxy_healthy": True,
                                     "last_action": "readded", "reason": "proxy healthy > stable",
                                     "at": time.strftime("%Y-%m-%d %H:%M:%S")})
            log("[guardian] 프록시 정상 복구 → env 재투입. 로깅 재개.")

        time.sleep(proxyguard.GUARDIAN_TICK_S)


def _collector_stale(cfg) -> bool:
    """컬렉터 생존 판정. 컬렉터가 매 루프 갱신하는 alive 파일(collector.alive) mtime 으로 본다.
    (로그 mtime 은 이벤트 있을 때만 갱신돼 유휴 컬렉터를 죽은 걸로 오판 → 중복 기동 유발하므로 안 씀.)
    파일이 60s 넘게 안 갱신되거나 없으면 stale."""
    from . import proxyguard
    alive = proxyguard.data_dir() / "collector.alive"
    try:
        age = __import__("time").time() - alive.stat().st_mtime
    except OSError:
        return True  # alive 파일 없음 = 컬렉터 미가동(또는 구버전) → 기동 유도
    return age > 60.0


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
    if os.name == "nt":
        # 옛 schtasks 설치 흔적도 제거(있으면).
        subprocess.call(["schtasks", "/End", "/TN", a.task_name],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.call(["schtasks", "/Delete", "/TN", a.task_name, "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
