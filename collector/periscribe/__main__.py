"""CLI 엔트리. `python -m periscribe`

옵션은 config.json / 환경변수(PERISCRIBE_*) / 커맨드라인 플래그 순으로 덮어쓴다.
"""

from __future__ import annotations

import argparse
import sys

from .collector import Collector
from .config import Config
from .sink import StdoutSink, SupabaseSink


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="periscribe", description="Claude Code transcript -> Supabase collector")
    p.add_argument("-c", "--config", default="config.json", help="설정 파일 경로(기본: config.json)")
    p.add_argument("--watch-dir", help="감시 디렉터리 override")
    p.add_argument("--machine-id", help="machine_id override")
    p.add_argument("--poll-interval", type=float, help="폴링 주기(초)")
    p.add_argument("--backfill", type=int, help="기존 파일 마지막 N줄 백필(기본 0=EOF부터)")
    p.add_argument("--store-raw", action="store_true", help="원본 라인을 events.raw에 저장")
    p.add_argument("--redact", action="store_true", help="수집 단계 민감정보 마스킹")
    p.add_argument("--dry-run", action="store_true", help="Supabase 대신 stdout으로 이벤트 출력(테스트)")
    args = p.parse_args(argv)

    cfg = Config.load(args.config)
    # 커맨드라인 override
    if args.watch_dir:
        cfg.watch_dir = args.watch_dir
    if args.machine_id:
        cfg.machine_id = args.machine_id
    if args.poll_interval is not None:
        cfg.poll_interval = args.poll_interval
    if args.backfill is not None:
        cfg.backfill = args.backfill
    if args.store_raw:
        cfg.store_raw = True
    if args.redact:
        cfg.redact = True

    if args.dry_run:
        sink = StdoutSink()
    else:
        try:
            cfg.validate()
        except ValueError as e:
            print(f"[periscribe] 설정 오류: {e}", file=sys.stderr)
            return 2
        sink = SupabaseSink(cfg.supabase_url, cfg.supabase_key, cfg.table)

    Collector(cfg, sink).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
