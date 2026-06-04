"""Tailer — 파일 하나를 오프셋 기반으로 tail.

처리(spec §7):
- 미완성 마지막 줄: 개행으로 끝나지 않으면 보류, 완성되면 정확히 한 번만 처리.
- 파일 회전/트렁케이트: inode 변경 시 오프셋 0, 크기 < 오프셋이면 0 리셋.
- 오프셋은 "확정된" 바이트 위치. 호출자가 적재 성공 후 commit() 한다.
"""

from __future__ import annotations

import os
from pathlib import Path


def file_inode(path: str) -> int:
    """플랫폼 독립 inode 식별자. Windows는 st_ino가 0일 수 있어 보조로 ctime 사용."""
    st = os.stat(path)
    ino = getattr(st, "st_ino", 0)
    if ino:
        return ino
    # Windows 폴백: 생성시각 기반 의사 inode
    return int(st.st_ctime_ns)


class Tailer:
    """한 파일의 읽기 상태. read_new_lines() -> (lines, new_offset)."""

    def __init__(self, path: str, offset: int = 0, inode: int | None = None) -> None:
        self.path = path
        self.offset = offset
        self.inode = inode if inode is not None else self._safe_inode()
        # 마지막 commit 된 오프셋(적재 성공 지점). 시작은 offset과 동일.
        self.committed_offset = offset

    def _safe_inode(self) -> int:
        try:
            return file_inode(self.path)
        except OSError:
            return 0

    def _check_rotation(self) -> None:
        """회전/트렁케이트 감지 후 필요 시 오프셋 리셋."""
        try:
            st = os.stat(self.path)
        except OSError:
            return
        cur_inode = file_inode(self.path)
        if cur_inode != self.inode:
            # 파일 교체(회전) -> 처음부터
            self.inode = cur_inode
            self.offset = 0
            self.committed_offset = 0
            return
        if st.st_size < self.offset:
            # 트렁케이트 -> 0 리셋
            self.offset = 0
            self.committed_offset = 0

    def read_new_lines(self) -> tuple[list[str], int]:
        """오프셋 이후의 완성된(개행으로 끝나는) 줄들을 읽는다.

        반환: (완성된 줄 리스트, 그 줄들 끝 바이트 오프셋).
        미완성 마지막 줄은 포함하지 않으며 오프셋도 그 앞까지만 전진 후보다.
        실제 영속은 호출자가 commit()로 확정한다.
        """
        self._check_rotation()
        try:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except OSError:
            return [], self.offset

        if not chunk:
            return [], self.offset

        # 마지막이 개행이 아니면 마지막(미완성) 줄은 보류
        last_nl = chunk.rfind(b"\n")
        if last_nl == -1:
            # 완성된 줄이 하나도 없음
            return [], self.offset

        complete = chunk[: last_nl + 1]
        consumed = len(complete)
        new_offset = self.offset + consumed

        text = complete.decode("utf-8", errors="replace")
        lines = [ln for ln in text.split("\n") if ln != ""]
        return lines, new_offset

    def commit(self, new_offset: int) -> None:
        """적재 성공 후 호출. 오프셋을 확정 전진."""
        self.offset = new_offset
        self.committed_offset = new_offset


def initial_offset(path: str, backfill_lines: int) -> int:
    """기존 파일 시작 오프셋 계산.

    backfill_lines == 0: EOF부터(과거 폭주 방지).
    backfill_lines > 0:  마지막 N개의 완성된 줄을 백필하도록 그 시작 바이트 반환.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    if backfill_lines <= 0:
        return size

    # 끝에서부터 N개의 개행 경계를 찾는다(단순/견고 우선: 끝에서 블록 단위로 역탐색)
    want = backfill_lines
    block = 64 * 1024
    pos = size
    newline_positions: list[int] = []
    with open(path, "rb") as f:
        while pos > 0 and len(newline_positions) <= want:
            read_size = min(block, pos)
            pos -= read_size
            f.seek(pos)
            data = f.read(read_size)
            for i in range(len(data) - 1, -1, -1):
                if data[i] == 0x0A:  # '\n'
                    newline_positions.append(pos + i)
                    if len(newline_positions) > want:
                        break
    if len(newline_positions) <= want:
        return 0  # 파일 전체가 N줄 이하
    # newline_positions[want] 는 마지막 N줄 직전의 개행 위치
    return newline_positions[want] + 1
