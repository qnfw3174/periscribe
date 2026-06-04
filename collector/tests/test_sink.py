"""Sink 회귀 테스트: NUL 제거 + 배치 키 정규화."""

import json

from periscribe.sink import _strip_nul, _normalize_rows


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
