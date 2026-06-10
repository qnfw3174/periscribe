"""proxypolicy — API 프록시 요청측 통제(차단/레닥션/시스템프롬프트 주입).

호스트의 proxy-policy.json(편집 가능, 매 요청 핫리로드)을 읽어 Anthropic 으로 가는 요청을
검사·수정한다. 잘못된 정책/누락은 통제 미적용(통과)으로 폴백 — 통제가 Claude 를 깨지 않게.
응답 스트림은 절대 건드리지 않는다(요청측만). 표준 라이브러리만.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

DEFAULT_POLICY = {
    "enabled": True,
    "block_patterns": [],      # 요청 내용 매치 시 차단(에러 반환, Anthropic 미전송)
    "redact_patterns": [],     # 전송 전 messages 텍스트에서 추가 마스킹
    "inject_system": "",       # system 프롬프트에 가드레일 텍스트 append
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


def _user_text(body: dict[str, Any]) -> str:
    """모든 user 메시지의 실제 입력(프롬프트 텍스트 + tool_result 내용) 결합. 끼워넣은 컨텍스트 블록 제외.
    (Claude는 실제 프롬프트를 messages[0] 텍스트블록에 두고 messages[-1]에 system 컨텍스트를 붙이므로
    마지막 메시지만 보면 안 됨.)"""
    parts = []
    for msg in body.get("messages") or []:
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


def apply_policy(policy: dict[str, Any], body: dict[str, Any]) -> tuple[bool, dict[str, Any], str]:
    """(blocked, modified_body, reason). blocked면 modified_body 무의미(전송 안 함)."""
    if not isinstance(body, dict) or not policy.get("enabled", True):
        return False, body, ""

    # 1) 차단: user 입력(프롬프트/tool_result) 내용이 패턴에 매치되면 차단.
    hay = _user_text(body)
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
