"""proxypolicy — API 프록시 통제(요청 차단/레닥션/주입 + 응답측 도구호출 게이팅).

호스트의 proxy-policy.json(편집 가능, 매 요청 핫리로드)을 읽어 Anthropic 으로 가는 요청과
돌아오는 응답을 검사·수정한다. 잘못된 정책/누락은 통제 미적용(통과)으로 폴백 — 통제가 Claude 를
깨지 않게(fail-open). 표준 라이브러리만.

- 요청측 차단: 매 요청의 '신규(trailing) 프롬프트'만 검사(히스토리 재차단으로 세션이 막히지 않게).
  차단 시 프록시가 합성 응답(block_message)으로 직접 대답 → Anthropic 미전송, 세션 무손상.
- 응답측 게이팅: 응답의 위험 tool_use(예: 파일 삭제)를 탐지해 실행 전에 차단(apiproxy 가 수행).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

DEFAULT_POLICY = {
    "enabled": True,
    "block_patterns": [],          # 신규 프롬프트 매치 시 차단(프록시가 block_message 로 대답, Anthropic 미전송)
    "block_message": "취소",        # 차단 시 프록시가 합성해 보낼 assistant 응답 텍스트
    "redact_patterns": [],         # 전송 전 messages 텍스트에서 추가 마스킹
    "inject_system": "",           # system 프롬프트에 가드레일 텍스트 append
    "gate_tool_use": False,        # 응답측 도구호출 게이팅 ON/OFF(켜면 응답을 버퍼해 검사)
    "tool_block_patterns": [],     # 위험 도구호출 패턴(매치 대상: "<name> <input json>")
    "tool_block_message": "파일 삭제 - 차단",  # 도구 차단 시 프록시가 대답할 텍스트
}


def load_policy(path: str) -> dict[str, Any]:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return dict(DEFAULT_POLICY)


def ensure_policy_file(path: str) -> None:
    p = Path(path)
    if not p.exists():
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(DEFAULT_POLICY, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass


def _compile(patterns: Any) -> list[re.Pattern]:
    out = []
    if isinstance(patterns, list):
        for p in patterns:
            if isinstance(p, str) and p:
                try:
                    out.append(re.compile(p, re.I))
                except re.error:
                    pass
    return out


def _is_ctx(t: str) -> bool:
    t = (t or "").lstrip()
    return (t.startswith("<system-reminder>") or t.startswith("<command-message>")
            or t.startswith("<command-name>") or t.startswith("<local-command"))


def _trailing_user_text(body: dict[str, Any]) -> str:
    """'마지막 assistant 이후'(trailing) user 입력만 결합 — 신규 프롬프트/tool_result. 컨텍스트 블록 제외.

    전체 히스토리를 보면 안 된다: 한 번 차단된 프롬프트가 Claude 히스토리에 남아 매 요청 재전송되는데,
    전체를 검사하면 매번 재차단되어 세션이 영구히 막힌다. 차단 시 프록시가 합성 assistant 응답을 끼워
    넣으므로(apiproxy) 그 프롬프트는 assistant 경계 뒤로 밀려나 다음 요청의 trailing 에서 빠진다 →
    "다음 프롬프트는 정상" 이 성립. (apilog.events_from_request 의 trailing 판정과 동일 의미.)"""
    msgs = body.get("messages") or []
    last_asst = -1
    for i, m in enumerate(msgs):
        if isinstance(m, dict) and m.get("role") == "assistant":
            last_asst = i
    parts = []
    for msg in msgs[last_asst + 1:]:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        c = msg.get("content")
        blocks = [c] if isinstance(c, str) else (c if isinstance(c, list) else [])
        for b in blocks:
            if isinstance(b, str):
                if not _is_ctx(b):
                    parts.append(b)
            elif isinstance(b, dict):
                if b.get("type") == "text" and isinstance(b.get("text"), str):
                    if not _is_ctx(b["text"]):
                        parts.append(b["text"])
                elif b.get("type") == "tool_result":
                    tc = b.get("content")
                    parts.append(tc if isinstance(tc, str) else json.dumps(tc, ensure_ascii=False, default=str))
    return "\n".join(parts)


def match_blocked_tools(blocks: Any, patterns: Any) -> list[str]:
    """응답 blocks(assemble_sse/message_from_json 형태) 중 tool_block_patterns 에 매치되는 tool_use 의
    이름 목록. 매치 대상은 "<name> <input json>". apiproxy 의 응답 게이팅이 호출(순수 함수)."""
    rxs = _compile(patterns)
    if not rxs:
        return []
    hits: list[str] = []
    for b in blocks or []:
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        hay = f"{b.get('name', '')} {json.dumps(b.get('input') or {}, ensure_ascii=False, default=str)}"
        if any(rx.search(hay) for rx in rxs):
            hits.append(b.get("name") or "tool")
    return hits


def apply_policy(policy: dict[str, Any], body: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
    """(blocked, modified_body, reason). blocked면 modified_body 무의미(전송 안 함)."""
    if not isinstance(body, dict) or not policy.get("enabled", True):
        return False, body, ""

    # 1) 차단: 신규(trailing) user 입력(프롬프트/tool_result)이 패턴에 매치되면 차단.
    hay = _trailing_user_text(body)
    for rx in _compile(policy.get("block_patterns")):
        m = rx.search(hay)
        if m:
            return True, body, f"block_pattern: {rx.pattern}"

    modified = body
    # 2) 레닥션: 전송 본문의 messages 텍스트에서 패턴 마스킹(비밀이 Anthropic 으로 안 나가게).
    rxs = _compile(policy.get("redact_patterns"))
    if rxs:
        modified = json.loads(json.dumps(body, ensure_ascii=False, default=str))  # 깊은 복사
        _redact_messages(modified, rxs)

    # 3) 시스템 프롬프트 주입(가드레일 append).
    inj = policy.get("inject_system")
    if isinstance(inj, str) and inj.strip():
        if modified is body:
            modified = json.loads(json.dumps(body, ensure_ascii=False, default=str))
        modified["system"] = _append_system(modified.get("system"), inj)

    return False, modified, ""


def _redact_messages(body: dict[str, Any], rxs: list[re.Pattern]) -> None:
    def red(s: str) -> str:
        out = s
        for rx in rxs:
            out = rx.sub("[REDACTED]", out)
        return out
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        c = msg.get("content")
        if isinstance(c, str):
            msg["content"] = red(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and isinstance(b.get("text"), str):
                    b["text"] = red(b["text"])


def _append_system(system: Any, inj: str) -> Any:
    if system is None or system == "":
        return inj
    if isinstance(system, str):
        return system + "\n\n" + inj
    if isinstance(system, list):
        return system + [{"type": "text", "text": inj}]
    return system
