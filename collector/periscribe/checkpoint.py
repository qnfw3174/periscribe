"""Checkpoint — 파일별 오프셋 영속.

규율(spec §6.5): 오프셋은 Supabase 적재 확정 후에만 디스크에 영속한다.
키는 파일 경로 + inode(회전 감지용). 원자적 쓰기(temp -> replace).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class Checkpoint:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if self.path.is_file():
            try:
                self._state = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._state = {}

    def get(self, file_path: str) -> dict[str, Any] | None:
        """{'offset': int, 'inode': int} 또는 None."""
        return self._state.get(file_path)

    def set(self, file_path: str, offset: int, inode: int) -> None:
        self._state[file_path] = {"offset": offset, "inode": inode}
        self._flush()

    def _flush(self) -> None:
        # 원자적 쓰기: 같은 디렉터리에 temp 작성 후 os.replace
        d = self.path.parent
        fd, tmp = tempfile.mkstemp(dir=str(d), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
