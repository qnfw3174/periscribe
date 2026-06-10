"""API 게이트웨이 — apilog 매핑/SSE 조립/세션지문, proxypolicy 통제, proxycert 생성 테스트.
네트워크/실제 Claude 불필요(순수 함수). 프록시 HTTP 흐름은 수동 E2E로 검증."""
import json

from periscribe import apilog, proxypolicy, proxycert

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
    up_id = evs[0]["event_id"]

    # 다음 턴(tool_result). 이전 프롬프트가 재전송돼도 같은 event_id 라 dedup.
    body2 = {"system": "S", "messages": [
        {"role": "user", "content": [{"type": "text", "text": "list files"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a.txt\nb.txt"}]},
    ]}
    evs2 = apilog.events_from_request(body2, "PC", sid)
    kinds = sorted(e["kind"] for e in evs2)
    assert kinds == ["tool_result", "user_prompt"]   # 재전송 프롬프트 + 새 tool_result
    up2 = next(e for e in evs2 if e["kind"] == "user_prompt")
    assert up2["event_id"] == up_id                  # 같은 프롬프트 → 같은 id(멱등 dedup)
    tr = next(e for e in evs2 if e["kind"] == "tool_result")
    assert tr["tool_use_id"] == "t1" and tr["payload"]["output_full"] == "a.txt\nb.txt"

    # 세션 지문(폴백): 같은 system+messages[0] → 같은 세션 / 다르면 다른 세션
    a = {"system": "S", "messages": [{"role": "user", "content": "hi"}]}
    b = {"system": "S", "messages": [{"role": "user", "content": "different"}]}
    assert apilog.session_id_for(a) != apilog.session_id_for(b)
    assert apilog.session_id_for(a) == apilog.session_id_for(dict(a))


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
