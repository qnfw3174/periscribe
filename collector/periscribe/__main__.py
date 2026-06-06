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
    """창(GUI)으로 토큰을 입력받아 설치. tkinter가 없으면 콘솔(cmd_setup)로 폴백."""
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
    return 0


def main(argv: list[str] | None = None) -> int:
    # windowed(콘솔 없는) 빌드에선 stdout/stderr가 None일 수 있어 print()가 죽는다. 더미로 대체.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

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
        # 단일 exe 더블클릭: GUI 설치 창(설치돼 있으면 재설치 안내도 GUI에서).
        if getattr(sys, "frozen", False):
            return gui_setup()
        # 소스 실행(개발): 기존처럼 로컬 config.json 으로 run.
        return cmd_run([])

    # 알 수 없는 첫 인자 → run 옵션으로 취급(기존 호환: `periscribe --dry-run` 등).
    return cmd_run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
