"""Sink 회귀 테스트: NUL 제거 + 배치 키 정규화 + poison 이분탐색 격리."""

import json

from periscribe.sink import _strip_nul, _normalize_rows, IngestSink, SinkDataError


def test_strip_nul_removes_real_nul():
    s = _strip_nul("a" + chr(0) + "b")
    assert s == "ab"
    assert chr(0) not in s


def test_strip_nul_preserves_literal_backslash_u0000():
    # 보안 분석 transcript 등에는 "널바이트"를 설명하는 리터럴 텍스트가 들어옴.
    # 실제 NUL(chr 0)만 제거하고, 6글자 텍스트  은 보존해야 한다(JSON도 안 깨짐).
    literal = "see " + chr(92) + "u0000 in text"
    out = _strip_nul("real" + chr(0) + "; " + literal)
    assert chr(0) not in out
    assert (chr(92) + "u0000") in out
    # 직렬화 후 다시 파싱돼야 유효 JSON
    body = json.dumps({"t": out}, ensure_ascii=False).encode("utf-8")
    assert json.loads(body)["t"] == out


def test_strip_nul_recurses_dict_and_list():
    o = {"a": "x" + chr(0), "b": {"c": ["y" + chr(0), 1, None]}, "n": 3}
    assert _strip_nul(o) == {"a": "x", "b": {"c": ["y", 1, None]}, "n": 3}


def test_normalize_rows_unions_keys():
    # PostgREST 벌크 insert는 모든 객체 키가 같아야 함(PGRST102) -> 합집합 + None 채움.
    rows = _normalize_rows([
        {"event_id": "1", "tool": "Bash"},
        {"event_id": "2", "is_error": True},
    ])
    keys = {frozenset(r.keys()) for r in rows}
    assert len(keys) == 1
    assert keys.pop() == frozenset({"event_id", "tool", "is_error"})
    by_id = {r["event_id"]: r for r in rows}
    assert by_id["1"]["is_error"] is None
    assert by_id["2"]["tool"] is None


def test_emit_isolates_poison_row(monkeypatch):
    # 서버가 'bad' 행이 든 배치를 4xx로 거부(SinkDataError) → 이분탐색으로 그 행만 스킵,
    # 정상 행은 적재되고 예외는 전파되지 않아 오프셋이 전진(파일 정체 해소)한다.
    sink = IngestSink("http://x/ingest", "tok")
    posted = []

    def fake_post(rows):
        ids = [r.get("event_id") for r in rows]
        if "bad" in ids:
            raise SinkDataError("ingest HTTP 400: bad row")
        posted.extend(ids)
        return {}

    monkeypatch.setattr(sink, "_post", fake_post)
    sink.emit([{"event_id": "a"}, {"event_id": "bad"}, {"event_id": "c"}])
    assert "a" in posted and "c" in posted
    assert "bad" not in posted
    assert "bad" in sink.last_drop


def test_emit_network_error_propagates(monkeypatch):
    # 네트워크/5xx(SinkError)는 스킵하지 않고 전파 → 호출자가 재시도(오프셋 미전진).
    from periscribe.sink import SinkError
    sink = IngestSink("http://x/ingest", "tok")

    def fake_post(rows):
        raise SinkError("ingest 연결 실패")

    monkeypatch.setattr(sink, "_post", fake_post)
    try:
        sink.emit([{"event_id": "a"}])
        assert False, "SinkError 가 전파돼야 함"
    except SinkError:
        pass
