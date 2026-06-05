"""Collector 시작 오프셋 결정 회귀 테스트.

핵심: 최초 실행(과거 폭주 방지 EOF/backfill) vs 재시작(다운타임 중 새 세션은 처음부터).
"""

import json
from pathlib import Path

from periscribe.collector import Collector
from periscribe.config import Config


class DummySink:
    def emit(self, events):  # noqa: D401
        pass


def make_file(p: Path, n_lines: int) -> str:
    # 실제 transcript와 동일하게 LF(\n)로 기록(Windows write_text의 CRLF 변환 회피).
    content = "\n".join(f'{{"i":{i}}}' for i in range(n_lines)) + "\n"
    p.write_bytes(content.encode("utf-8"))
    return str(p)


def make_collector(tmp_path: Path, backfill: int = 0, checkpoint_data=None, container_root: str = "") -> Collector:
    cp = tmp_path / "cp.json"
    if checkpoint_data is not None:
        cp.write_text(json.dumps(checkpoint_data), encoding="utf-8")
    cfg = Config()
    cfg.watch_dir = str(tmp_path)
    cfg.checkpoint_path = str(cp)
    cfg.backfill = backfill
    cfg.container_root = container_root
    return Collector(cfg, DummySink())


def test_container_id_from_path(tmp_path):
    croot = tmp_path / "agents"
    f = croot / "ctr-A" / "projfolder" / "sess.jsonl"
    f.parent.mkdir(parents=True)
    f.write_bytes(b'{"i":0}\n')
    c = make_collector(tmp_path, container_root=str(croot))
    assert c._container_id_for(str(f)) == "ctr-A"
    # container_root 밖(native) → None
    native = tmp_path / "a.jsonl"; native.write_bytes(b"x")
    assert c._container_id_for(str(native)) is None
    # discover가 두 루트 모두 포함
    make_file(tmp_path / "n.jsonl", 1)
    found = c.discover()
    assert str(f) in found


def test_fresh_start_skips_history_at_eof(tmp_path):
    f = make_file(tmp_path / "a.jsonl", 5)
    size = Path(f).stat().st_size
    c = make_collector(tmp_path, backfill=0)
    assert c.fresh_start is True
    t = c._ensure_tailer(f, first_run=True)
    assert t.offset == size  # EOF: 기존 내용 스킵
    got, _ = t.read_new_lines()
    assert got == []


def test_fresh_start_backfill_reads_last_n(tmp_path):
    f = make_file(tmp_path / "a.jsonl", 5)
    c = make_collector(tmp_path, backfill=2)
    t = c._ensure_tailer(f, first_run=True)
    got, _ = t.read_new_lines()
    assert got == ['{"i":3}', '{"i":4}']  # 마지막 2줄만


def test_restart_new_file_read_from_start(tmp_path):
    # 이미 보던 파일(체크포인트 존재) -> 재시작으로 인식
    old = make_file(tmp_path / "old.jsonl", 3)
    c = make_collector(tmp_path, backfill=0, checkpoint_data={old: {"offset": 10, "inode": 0}})
    assert c.fresh_start is False
    # 다운타임 중 새로 생긴 세션 파일(체크포인트 없음) -> EOF가 아니라 처음부터(gap 수정)
    new = make_file(tmp_path / "new.jsonl", 4)
    t = c._ensure_tailer(new, first_run=True)
    assert t.offset == 0
    got, _ = t.read_new_lines()
    assert len(got) == 4


def test_midrun_new_file_read_from_start(tmp_path):
    f = make_file(tmp_path / "a.jsonl", 5)
    c = make_collector(tmp_path, backfill=0)
    t = c._ensure_tailer(f, first_run=False)  # 실행 중 발견
    assert t.offset == 0
    got, _ = t.read_new_lines()
    assert len(got) == 5


def test_saved_checkpoint_resumes_from_offset(tmp_path):
    f = make_file(tmp_path / "a.jsonl", 5)
    c = make_collector(tmp_path, backfill=0, checkpoint_data={f: {"offset": 7, "inode": 123}})
    t = c._ensure_tailer(f, first_run=True)
    assert t.offset == 7  # 저장된 오프셋 우선(backfill/EOF 무시)
