"""API 게이트웨이 — apilog 매핑/SSE 조립/세션지문, proxypolicy 통제, proxycert 생성 테스트.
네트워크/실제 Claude 불필요(순수 함수). 프록시 HTTP 흐름은 수동 E2E로 검증."""
import json

from periscribe import apilog, apiproxy, proxypolicy, proxycert

SSE = "\n".join([
    'event: message_start',
    'data: {"type":"message_start","message":{"id":"msg_01ABC","role":"assistant","content":[]}}',
    '',
    'event: content_block_start',
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
    'event: content_block_delta',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello "}}',
    'event: content_block_delta',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"world"}}',
    'event: content_block_stop',
    'data: {"type":"content_block_stop","index":0}',
    'event: content_block_start',
    'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"toolu_1","name":"Bash","input":{}}}',
    'event: content_block_delta',
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"command\\":\\"ls"}}',
    'event: content_block_delta',
    'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":" -la\\"}"}}',
    'event: content_block_stop',
    'data: {"type":"content_block_stop","index":1}',
    'event: message_stop',
    'data: {"type":"message_stop"}',
])


def test_assemble_sse_and_response_events():
    msg = apilog.assemble_sse(SSE)
    assert msg["id"] == "msg_01ABC"
    types = [b["type"] for b in msg["blocks"]]
    assert types == ["text", "tool_use"]
    assert msg["blocks"][0]["text"] == "Hello world"
    assert msg["blocks"][1]["name"] == "Bash" and msg["blocks"][1]["input"]["command"] == "ls -la"

    evs = apilog.events_from_message(msg, "PC", "api-sess1")
    assert {e["kind"] for e in evs} == {"assistant_text", "tool_use"}
    at = next(e for e in evs if e["kind"] == "assistant_text")
    assert at["source"] == "api" and at["payload"]["text"] == "Hello world" and at["event_id"] == "api-msg_01ABC#0"
    tu = next(e for e in evs if e["kind"] == "tool_use")
    assert tu["tool"] == "Bash" and tu["tool_use_id"] == "toolu_1" and tu["payload"]["command"] == "ls -la"
    # 멱등: 같은 입력 → 같은 event_id
    assert apilog.events_from_message(msg, "PC", "api-sess1")[0]["event_id"] == at["event_id"]


def test_request_events_and_session():
    sid = "api-sess1"
    # claude 실제 구조 모사: user 메시지가 [컨텍스트 블록, 실제 프롬프트 블록] + 끝에 system 메시지.
    body1 = {"system": "S", "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "<system-reminder>\nbig injected context...\n</system-reminder>"},
            {"type": "text", "text": "list files"}]},
        {"role": "system", "content": "skills context..."},
    ]}
    evs = apilog.events_from_request(body1, "PC", sid)
    # 컨텍스트(<system-reminder>)·system 메시지는 제외, 실제 프롬프트만
    assert len(evs) == 1 and evs[0]["kind"] == "user_prompt" and evs[0]["payload"]["text"] == "list files"

    # 다음 턴(tool_result). 재전송된 과거 프롬프트는 trailing(마지막 assistant 이후)이 아니라 아예 미생성.
    body2 = {"system": "S", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "list files"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.txt\nb.txt"}]},
    ]}
    evs2 = apilog.events_from_request(body2, "PC", sid)
    assert [e["kind"] for e in evs2] == ["tool_result"]   # 새 tool_result 만(히스토리 미재생성)
    tr = evs2[0]
    assert tr["tool_use_id"] == "t1" and tr["payload"]["output_full"] == "a.txt\nb.txt"

    # 세션 지문(폴백): 같은 system+messages[0] → 같은 세션 / 다르면 다른 세션
    a = {"system": "S", "messages": [{"role": "user", "content": "hi"}]}
    b = {"system": "S", "messages": [{"role": "user", "content": "different"}]}
    assert apilog.session_id_for(a) != apilog.session_id_for(b)
    assert apilog.session_id_for(a) == apilog.session_id_for(dict(a))


def test_trailing_delta_excludes_history():
    """소급 로깅 회귀 방지의 핵심: 프록시 OFF 기간 턴(u1, u2)이 ON 후 첫 요청의 히스토리로
    실려 와도 이벤트화되면 안 된다 — 마지막 assistant 이후(u3)만."""
    body = {"messages": [
        {"role": "user", "content": "u1 (off 기간)"},
        {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
        {"role": "user", "content": "u2 (off 기간)"},
        {"role": "assistant", "content": [{"type": "text", "text": "a2"}]},
        {"role": "user", "content": "u3 (on 이후 새 프롬프트)"},
    ]}
    evs = apilog.events_from_request(body, "PC", "s")
    assert len(evs) == 1 and evs[0]["payload"]["text"] == "u3 (on 이후 새 프롬프트)"
    texts = [e["payload"].get("text", "") for e in evs]
    assert not any("u1" in t or "u2" in t for t in texts)


def test_first_request_without_assistant_logs_all():
    # 세션 첫 요청(assistant 없음) → 전체 user 처리
    body = {"messages": [{"role": "user", "content": "first prompt"}]}
    evs = apilog.events_from_request(body, "PC", "s")
    assert len(evs) == 1 and evs[0]["payload"]["text"] == "first prompt"


def test_trailing_multiple_user_messages_all_captured():
    # 툴 실행 중 큐잉된 프롬프트: 마지막 assistant 이후 user 메시지 2개(tool_result + 새 프롬프트) 모두 캡처
    body = {"messages": [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "out"}]},
        {"role": "user", "content": "queued prompt"},
    ]}
    evs = apilog.events_from_request(body, "PC", "s")
    kinds = sorted(e["kind"] for e in evs)
    assert kinds == ["tool_result", "user_prompt"]
    assert next(e for e in evs if e["kind"] == "user_prompt")["payload"]["text"] == "queued prompt"
    assert not any("old" in str(e.get("payload")) for e in evs)


def test_blocked_marker_when_no_trailing_text():
    # 차단됐는데 trailing 에 user 텍스트가 없으면(정책이 과거에 매칭 등) 내용 없는 마커 1건 —
    # 과거 내용을 소급 로깅하지 않으면서 차단 사실은 남긴다.
    body = {"messages": [
        {"role": "user", "content": "secret history"},
        {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
    ]}
    evs = apilog.events_from_request(body, "PC", "s", blocked=True, block_reason="pat")
    assert len(evs) == 1
    ev = evs[0]
    assert ev["payload"]["blocked"] is True and ev["payload"]["block_reason"] == "pat"
    assert ev["is_error"] is True
    assert "secret history" not in ev["payload"]["text"]

    # trailing 에 텍스트가 있으면 그 이벤트에 blocked 플래그가 붙고 마커는 안 생긴다
    body2 = {"messages": [{"role": "user", "content": "bad prompt"}]}
    evs2 = apilog.events_from_request(body2, "PC", "s", blocked=True, block_reason="pat")
    assert len(evs2) == 1 and evs2[0]["payload"]["text"] == "bad prompt" and evs2[0]["payload"]["blocked"] is True


def test_session_from_metadata_user_id():
    # Claude는 metadata.user_id(JSON)에 안정적인 session_id 를 싣는다 → 지문보다 우선·안정.
    uid = '{"device_id":"d1","account_uuid":"acc","session_id":"7f2a9a96-uuid"}'
    body1 = {"metadata": {"user_id": uid}, "system": "X", "messages": [{"role": "user", "content": "turn1"}]}
    body2 = {"metadata": {"user_id": uid}, "system": "X", "messages": [
        {"role": "user", "content": "turn1"}, {"role": "assistant", "content": []},
        {"role": "user", "content": "turn2 different"}]}  # 다음 턴(messages 변함) — 그래도 같은 세션
    # 접두사 없이 raw session_id → transcript(같은 id)와 한 세션으로 병합
    assert apilog.session_id_for(body1) == "7f2a9a96-uuid"
    assert apilog.session_id_for(body2) == "7f2a9a96-uuid"  # metadata 동일 → 같은 세션


def test_policy_block_redact_inject():
    body = {"system": "base", "messages": [{"role": "user", "content": "please run rm -rf / now"}]}
    # block
    blocked, _, reason = proxypolicy.apply_policy({"enabled": True, "block_patterns": [r"rm\s+-rf"]}, body)
    assert blocked and "rm" in reason
    # redact (전송 본문 마스킹)
    b2 = {"messages": [{"role": "user", "content": "secret TOKEN=abc123 here"}]}
    blk, mod, _ = proxypolicy.apply_policy({"enabled": True, "redact_patterns": [r"TOKEN=\S+"]}, b2)
    assert not blk and "[REDACTED]" in mod["messages"][0]["content"] and "abc123" not in json.dumps(mod)
    assert "abc123" in b2["messages"][0]["content"]  # 원본 불변(깊은 복사)
    # inject_system
    blk, mod, _ = proxypolicy.apply_policy({"enabled": True, "inject_system": "GUARDRAIL"}, {"system": "X", "messages": []})
    assert "GUARDRAIL" in mod["system"]
    # disabled → 통과
    blk, mod, _ = proxypolicy.apply_policy({"enabled": False, "block_patterns": [r"rm"]}, body)
    assert not blk


def test_block_matches_trailing_only():
    """Section 1: 차단은 신규(trailing) 프롬프트만 — 히스토리의 aaaa 는 재차단 안 함(세션 안 막힘)."""
    pol = {"enabled": True, "block_patterns": ["aaaa"]}
    # aaaa 가 마지막 assistant 이전(히스토리) → 미차단
    body = {"messages": [
        {"role": "user", "content": "aaaa"},
        {"role": "assistant", "content": [{"type": "text", "text": "취소"}]},
        {"role": "user", "content": "hello"},
    ]}
    assert proxypolicy.apply_policy(pol, body)[0] is False
    # aaaa 가 trailing(마지막 assistant 이후) → 차단
    body2 = {"messages": [
        {"role": "user", "content": "aaaa"},
        {"role": "assistant", "content": [{"type": "text", "text": "x"}]},
        {"role": "user", "content": "please aaaa now"},
    ]}
    assert proxypolicy.apply_policy(pol, body2)[0] is True


def test_build_message_response_stream_and_json():
    """Section 1/2: 합성 응답이 Anthropic 형식과 호환(assemble_sse 로 되읽힘) + model 에코 + end_turn."""
    ct, body = apiproxy.build_message_response("claude-x", [], "취소", True)
    assert "event-stream" in ct
    s = body.decode("utf-8")
    assert "취소" in s and "message_stop" in s and "claude-x" in s
    assert apilog.assemble_sse(s)["blocks"][0]["text"] == "취소"
    # 비스트림 JSON + 선행 텍스트 보존 + 말미 notice
    ct2, body2 = apiproxy.build_message_response("claude-x", ["선행"], "파일 삭제 - 차단", False)
    assert "application/json" in ct2
    d = json.loads(body2.decode("utf-8"))
    assert d["stop_reason"] == "end_turn" and d["role"] == "assistant"
    assert [b["text"] for b in d["content"]] == ["선행", "파일 삭제 - 차단"]
    assert all(b["type"] != "tool_use" for b in d["content"])   # tool_use 없음 → 실행 불가


def test_match_blocked_tools():
    """Section 2: 위험 tool_use(rm) 탐지, 안전 도구(ls)는 통과, 패턴 없으면 전부 통과."""
    blocks = [
        {"type": "text", "text": "지울게요"},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "rm -rf /tmp/x"}},
    ]
    assert proxypolicy.match_blocked_tools(blocks, [r"\brm\b"]) == ["Bash"]
    safe = [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "ls -la"}}]
    assert proxypolicy.match_blocked_tools(safe, [r"\brm\b"]) == []
    assert proxypolicy.match_blocked_tools(blocks, []) == []


def test_proxycert_generates_valid(tmp_path):
    from cryptography import x509
    paths = proxycert.ensure_certs(tmp_path)
    for k in ("ca_pem", "server_pem", "server_key"):
        assert paths[k]
    server = x509.load_pem_x509_certificate(open(paths["server_pem"], "rb").read())
    ca = x509.load_pem_x509_certificate(open(paths["ca_pem"], "rb").read())
    # 리프는 CA가 서명(issuer == CA subject)
    assert server.issuer == ca.subject
    san = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    ips = [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    assert "127.0.0.1" in ips
    # 재호출 시 재사용(파일 유지)
    paths2 = proxycert.ensure_certs(tmp_path)
    assert paths2["server_pem"] == paths["server_pem"]
