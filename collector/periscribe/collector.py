"""Collector — 폴링 루프로 감시 디렉터리의 *.jsonl 을 tail -> 파싱 -> 적재.

흐름(spec §3.3, §6):
1. watch_dir 하위 *.jsonl 발견(새 파일 포함).
2. 각 파일에서 새 완성 줄을 읽어 파싱(파일별 오프셋).
3. 폴링 1회분 이벤트를 배치 insert(멱등).
4. insert 성공 후에만 오프셋 체크포인트 영속. 실패 시 전진 안 하고 다음 폴링에 재시도.
"""

from __future__ import annotations

import signal
import sys
import time
from pathlib import Path
from typing import Any

from .checkpoint import Checkpoint
from .config import Config
from .parser import Parser
from .sink import Sink, SinkError
from .tailer import Tailer, file_inode, initial_offset


class Collector:
    def __init__(self, config: Config, sink: Sink) -> None:
        self.config = config
        self.sink = sink
        self.parser = Parser(
            machine_id=config.machine_id,
            source=config.source,
            store_thinking=config.store_thinking,
            store_raw=config.store_raw,
            redact=config.redact,
        )
        self.checkpoint = Checkpoint(config.checkpoint_path)
        self.tailers: dict[str, Tailer] = {}
        self._running = False

    # ---- 파일 발견 ----
    def discover(self) -> list[str]:
        root = Path(self.config.watch_dir)
        if not root.exists():
            return []
        return [str(p) for p in root.rglob("*.jsonl")]

    def _project_folder(self, file_path: str) -> str:
        # ~/.claude/projects/<folder>/<session>.jsonl -> <folder>
        return Path(file_path).parent.name

    def _ensure_tailer(self, file_path: str, first_run: bool) -> Tailer:
        existing = self.tailers.get(file_path)
        if existing is not None:
            return existing

        saved = self.checkpoint.get(file_path)
        if saved is not None:
            tailer = Tailer(file_path, offset=saved.get("offset", 0), inode=saved.get("inode"))
        elif first_run:
            # 기존 파일: 기본 EOF부터(또는 backfill)
            off = initial_offset(file_path, self.config.backfill)
            try:
                ino = file_inode(file_path)
            except OSError:
                ino = 0
            tailer = Tailer(file_path, offset=off, inode=ino)
        else:
            # 실행 중 새로 생긴 파일: 처음부터
            tailer = Tailer(file_path, offset=0)

        self.tailers[file_path] = tailer
        return tailer

    # ---- 한 파일 1회 처리 ----
    def _process_file(self, file_path: str, first_run: bool) -> int:
        tailer = self._ensure_tailer(file_path, first_run)
        lines, new_offset = tailer.read_new_lines()
        if not lines:
            return 0

        project = self._project_folder(file_path)
        events: list[dict[str, Any]] = []
        for line in lines:
            events.extend(self.parser.parse_line(line, project))

        # 적재(배치). 실패하면 오프셋 전진 안 함 -> 다음 폴링 재시도(멱등이라 안전).
        if events:
            for batch in _chunks(events, self.config.batch_size):
                self.sink.emit(batch)  # 실패 시 SinkError 전파

        # 적재 확정 후에만 오프셋 영속
        tailer.commit(new_offset)
        self.checkpoint.set(file_path, new_offset, tailer.inode)
        return len(events)

    # ---- 메인 루프 ----
    def run(self) -> None:
        self._running = True
        self._install_signal_handlers()
        first_run = True
        log = _stderr
        log(f"[periscribe] watch={self.config.watch_dir} machine_id={self.config.machine_id}")
        log(f"[periscribe] poll={self.config.poll_interval}s backfill={self.config.backfill}")

        while self._running:
            try:
                files = self.discover()
                total = 0
                for fp in files:
                    try:
                        total += self._process_file(fp, first_run)
                    except SinkError as e:
                        # 네트워크/적재 실패: 이 파일 오프셋은 전진 안 됨. 다음 폴링 재시도.
                        log(f"[periscribe] sink 실패(재시도 예정): {e}")
                    except Exception as e:
                        # 파일 단위 예외가 전체 루프를 죽이지 않게
                        log(f"[periscribe] 파일 처리 오류 {fp}: {e}")
                if total:
                    log(f"[periscribe] +{total} events")
                first_run = False
            except Exception as e:
                log(f"[periscribe] 루프 오류: {e}")
            time.sleep(self.config.poll_interval)

        log("[periscribe] 종료")

    def stop(self, *_: Any) -> None:
        self._running = False

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.stop)
            except (ValueError, OSError):
                pass  # 메인 스레드가 아니면 무시


def _chunks(seq: list[Any], n: int):
    n = max(1, n)
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
