"""CLI 엔트리.

  periscribe                  (더블클릭/인자 없음) 미설치면 토큰만 입력받아 자동 설치, 설치됐으면 상태 안내
  periscribe setup            토큰 입력 → config 작성 + 작업 등록(대화형). 더블클릭과 동일
  periscribe [run] [옵션]     수집 루프 실행
  periscribe install ...      비대화형 설치(--token/--url). 자동화/스크립트용
  periscribe uninstall        작업 제거

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

# 배포본에 내장되는 기본 ingest 엔드포인트. 사용자는 토큰만 넣으면 된다(URL 입력 불필요).
# 빌드/실행 시 PERISCRIBE_DEFAULT_INGEST_URL 로 덮어쓸 수 있다.
DEFAULT_INGEST_URL = os.environ.get(
    "PERISCRIBE_DEFAULT_INGEST_URL",
    "https://wgzsjdmohbawfcxiicqc.supabase.co/functions/v1/ingest",
)


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
    print(f"[install] task '{a.task_name}' command: {run_cmd}")
    if a.dry_run:
        print("[install] --dry-run: 실제 변경 없음")
        return 0

    data.mkdir(parents=True, exist_ok=True)
    (data / "checkpoints").mkdir(exist_ok=True)
    (data / "logs").mkdir(exist_ok=True)
    config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    # 재설정 시 중복 실행 방지: 기존 작업이 돌고 있으면 먼저 정지(없으면 무시).
    subprocess.call(["schtasks", "/End", "/TN", a.task_name],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 작업 스케줄러 등록(로그온 시 자동 시작, 현재 사용자)
    rc = subprocess.call([
        "schtasks", "/Create", "/TN", a.task_name, "/TR", run_cmd,
        "/SC", "ONLOGON", "/RL", "LIMITED", "/F",
    ])
    if rc != 0:
        print(f"[install] 작업 등록 실패(schtasks rc={rc}). 관리자 권한 또는 정책 확인.", file=sys.stderr)
        return rc
    subprocess.call(["schtasks", "/Run", "/TN", a.task_name])
    print(f"[install] 완료. 헬스바에 곧 표시됩니다. 로그: {cfg['log_file']}")
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
    subprocess.call(["schtasks", "/End", "/TN", a.task_name])
    rc = subprocess.call(["schtasks", "/Delete", "/TN", a.task_name, "/F"])
    print("[uninstall] 작업 제거" + ("됨" if rc == 0 else f" 실패(rc={rc})"))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "install":
        return cmd_install(argv[1:])
    if argv and argv[0] == "uninstall":
        return cmd_uninstall(argv[1:])
    if argv and argv[0] == "setup":
        return cmd_setup(argv[1:])
    if argv and argv[0] == "run":
        return cmd_run(argv[1:])

    if not argv:
        # 단일 exe 더블클릭: 미설치면 대화형 설치, 설치돼 있으면 안내(중복 실행 방지).
        if getattr(sys, "frozen", False):
            if _is_installed():
                print("Periscribe가 이미 설치되어 백그라운드에서 실행 중입니다.")
                print("  토큰 재설정: periscribe.exe setup")
                print("  제거:       periscribe.exe uninstall")
                _pause()
                return 0
            return cmd_setup([])
        # 소스 실행(개발): 기존처럼 로컬 config.json 으로 run.
        return cmd_run([])

    # 알 수 없는 첫 인자 → run 옵션으로 취급(기존 호환: `periscribe --dry-run` 등).
    return cmd_run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
