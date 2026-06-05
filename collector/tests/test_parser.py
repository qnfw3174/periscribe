"""Parser 단위 테스트. 표준 라이브러리 + pytest.

핵심: 방어성(모르는 형태 skip), 멀티블록, tool_use/tool_result 상관, 멱등 키 유일성.
"""

import json

from periscribe.parser import Parser


def mk():
    return Parser(machine_id="test-pc", source="claude-code")


def line(obj):
    return json.dumps(obj)


def test_user_prompt_string():
    p = mk()
    evs = p.parse_line(line({
        "type": "user", "uuid": "u1", "sessionId": "s1", "timestamp": "2026-01-01T00:00:00Z",
        "message": {"content": "hello"},
    }))
    assert len(evs) == 1
    assert evs[0]["kind"] == "user_prompt"
    assert evs[0]["payload"]["text"] == "hello"
    assert evs[0]["event_id"] == "u1"
    assert evs[0]["machine_id"] == "test-pc"


def test_assistant_text_and_tool_use_multiblock():
    p = mk()
    evs = p.parse_line(line({
        "type": "assistant", "uuid": "a1", "sessionId": "s1", "timestamp": "t",
        "message": {"content": [
            {"type": "text", "text": "let me run it"},
            {"type": "tool_use", "id": "tu1", "name": "Bash",
             "input": {"command": "ls -la", "description": "list", "run_in_background": False}},
        ]},
    }))
    assert len(evs) == 2
    # 멀티블록 -> event_id 유일
    assert {e["event_id"] for e in evs} == {"a1#0", "a1#1"}
    text_ev = next(e for e in evs if e["kind"] == "assistant_text")
    assert text_ev["payload"]["text"] == "let me run it"
    bash_ev = next(e for e in evs if e["kind"] == "tool_use")
    assert bash_ev["tool"] == "Bash"
    assert bash_ev["tool_use_id"] == "tu1"
    assert bash_ev["payload"]["command"] == "ls -la"


def test_tool_use_non_bash_keeps_input():
    p = mk()
    evs = p.parse_line(line({
        "type": "assistant", "uuid": "a2", "sessionId": "s1", "timestamp": "t",
        "message": {"content": [
            {"type": "tool_use", "id": "tu2", "name": "Edit",
             "input": {"file_path": "/x", "old_string": "a", "new_string": "b"}},
        ]},
    }))
    assert len(evs) == 1
    assert evs[0]["tool"] == "Edit"
    assert evs[0]["payload"]["input"]["file_path"] == "/x"


def test_tool_result_array_content():
    p = mk()
    evs = p.parse_line(line({
        "type": "user", "uuid": "u2", "sessionId": "s1", "timestamp": "t",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu1", "is_error": False,
             "content": [{"type": "text", "text": "file1\nfile2"}]},
        ]},
    }))
    assert len(evs) == 1
    assert evs[0]["kind"] == "tool_result"
    assert evs[0]["tool_use_id"] == "tu1"
    assert evs[0]["is_error"] is False
    assert evs[0]["payload"]["output_full"] == "file1\nfile2"


def test_tool_result_error_string_content():
    p = mk()
    evs = p.parse_line(line({
        "type": "user", "uuid": "u3", "sessionId": "s1", "timestamp": "t",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu9", "is_error": True, "content": "boom"},
        ]},
    }))
    assert evs[0]["is_error"] is True
    assert evs[0]["payload"]["output_full"] == "boom"


def test_unknown_type_skipped():
    p = mk()
    assert p.parse_line(line({"type": "summary", "summary": "x"})) == []
    assert p.parse_line(line({"type": "system", "uuid": "z"})) == []
    assert p.parse_line(line({"type": "totally-new-type"})) == []


def test_garbage_never_raises():
    p = mk()
    assert p.parse_line("") == []
    assert p.parse_line("not json") == []
    assert p.parse_line("[1,2,3]") == []   # dict 아님
    assert p.parse_line("null") == []


def test_sidechain_fields():
    p = mk()
    evs = p.parse_line(line({
        "type": "assistant", "uuid": "a3", "sessionId": "s1", "timestamp": "t",
        "isSidechain": True, "agentId": "agent-x", "parentUuid": "p1",
        "message": {"content": [{"type": "text", "text": "sub"}]},
    }))
    assert evs[0]["is_sidechain"] is True
    assert evs[0]["agent_id"] == "agent-x"
    assert evs[0]["parent_uuid"] == "p1"


def test_thinking_ignored_by_default():
    p = mk()
    evs = p.parse_line(line({
        "type": "assistant", "uuid": "a4", "sessionId": "s1", "timestamp": "t",
        "message": {"content": [{"type": "thinking", "thinking": "hmm"}]},
    }))
    assert evs == []


def test_container_id_stamped():
    p = mk()
    evs = p.parse_line(line({
        "type": "user", "uuid": "u9", "sessionId": "s1", "timestamp": "t",
        "message": {"content": "hi"},
    }), project_folder="proj", container_id="demo-ctr")
    assert evs[0]["container_id"] == "demo-ctr"
    # 미지정이면 None(native 세션)
    evs2 = p.parse_line(line({
        "type": "user", "uuid": "u10", "sessionId": "s1", "timestamp": "t",
        "message": {"content": "hi"},
    }))
    assert evs2[0]["container_id"] is None


def test_redaction():
    p = Parser(machine_id="pc", redact=True)
    evs = p.parse_line(line({
        "type": "user", "uuid": "u4", "sessionId": "s1", "timestamp": "t",
        "message": {"content": "api_key=supersecretvalue123"},
    }))
    assert "supersecretvalue123" not in evs[0]["payload"]["text"]
    assert "[REDACTED]" in evs[0]["payload"]["text"]
