"""CLI 엔트리.

  periscribe [run] [옵션]     수집 루프 실행(기본)
  periscribe install ...      config 작성 + 작업 스케줄러 등록(부팅 자동실행)
  periscribe uninstall        작업 제거

옵션은 config.json / 환경변수(PERISCRIBE_*) / 커맨드라인 순으로 덮어쓴다.
단일 exe(PyInstaller)에서도 동일하게 동작한다(sys.frozen 감지).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import __version__
from .collector import Collector
from .config import Config
from .sink import IngestSink, StdoutSink

TASK_NAME = "PeriscribeCollector"


def _data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Periscribe"


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
    p.add_argument("--url", required=True, help="ingest 엔드포인트(.../functions/v1/ingest)")
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
    if argv and argv[0] == "run":
        argv = argv[1:]
    return cmd_run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
