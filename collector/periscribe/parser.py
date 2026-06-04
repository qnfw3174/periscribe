"""Parser — transcript 한 줄(JSON 텍스트) -> 0개 이상 정규화 이벤트(dict).

방어적 원칙(spec §2.5, §12):
- 빈 줄 / JSON 실패 / dict 아님 / 모르는 type -> 조용히 skip(예외 전파 금지).
- content 는 항상 배열로 보고 순회(멀티블록 폴백 대비).
- 모르는 형태도 키 존재 확인 + try/except 로 죽지 않는다.

멀티블록 한 줄은 같은 transcript uuid 를 공유한다. event_id 는 PK(멱등성 키)이므로
한 줄에서 여러 이벤트가 나오면 "<uuid>#<block_index>" 로 유일하게 만든다(재읽기 시 동일).
단일 이벤트만 나오면 uuid 그대로 사용한다.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

_REDACT_PATTERNS = [
    # 흔한 비밀 패턴(완벽하지 않음, 보조 수단)
    re.compile(r"(?i)(api[_-]?key|secret|password|passwd|token)\s*[=:]\s*\S+"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    out = text
    for pat in _REDACT_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


def _decode_project(folder: Optional[str]) -> Optional[str]:
    """URL 인코딩된 프로젝트 폴더명을 사람이 읽을 형태로 환원(보조)."""
    if not folder:
        return folder
    # "-Users-you-code-my-app" -> "/Users/you/code/my-app" (보조적, 원본도 보존됨)
    if folder.startswith("-"):
        return "/" + folder[1:].replace("-", "/")
    return folder


def _tool_result_text(content: Any) -> str:
    """tool_result content(문자열 또는 {type:text,text} 배열) -> 전문 문자열."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict):
                t = blk.get("text")
                if isinstance(t, str):
                    parts.append(t)
                elif "content" in blk:  # 중첩 방어
                    parts.append(_tool_result_text(blk.get("content")))
            elif isinstance(blk, str):
                parts.append(blk)
        return "\n".join(parts)
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


class Parser:
    def __init__(
        self,
        machine_id: str,
        source: str = "claude-code",
        store_thinking: bool = False,
        store_raw: bool = False,
        redact: bool = False,
        schema_version: int = 1,
    ) -> None:
        self.machine_id = machine_id
        self.source = source
        self.store_thinking = store_thinking
        self.store_raw = store_raw
        self.redact = redact
        self.schema_version = schema_version

    def parse_line(self, line: str, project_folder: Optional[str] = None) -> list[dict[str, Any]]:
        """한 줄 -> 이벤트 리스트. 절대 예외를 던지지 않는다."""
        try:
            return self._parse_line(line, project_folder)
        except Exception:
            # 모르는 형태/버그에도 수집 루프가 죽지 않도록 전부 흡수
            return []

    # ---- 내부 ----
    def _parse_line(self, line: str, project_folder: Optional[str]) -> list[dict[str, Any]]:
        line = line.strip()
        if not line:
            return []
        try:
            obj = json.loads(line)
        except Exception:
            return []
        if not isinstance(obj, dict):
            return []

        typ = obj.get("type")
        msg = obj.get("message")
        msg = msg if isinstance(msg, dict) else {}

        base = self._base_fields(obj, project_folder)
        raw = obj if self.store_raw else None

        if typ == "assistant":
            return self._from_assistant(msg, base, raw)
        if typ == "user":
            return self._from_user(msg, base, raw)
        if typ in ("summary", "system"):
            return []  # 메타/무관
        # 모르는 type -> skip
        return []

    def _base_fields(self, obj: dict[str, Any], project_folder: Optional[str]) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "machine_id": self.machine_id,
            "session_id": obj.get("sessionId"),
            "agent_id": obj.get("agentId"),
            "is_sidechain": bool(obj.get("isSidechain", False)),
            "parent_uuid": obj.get("parentUuid"),
            "ts": obj.get("timestamp"),
            "received_at": _now_iso(),
            "project": _decode_project(project_folder),
            "cwd": obj.get("cwd"),
            "_uuid": obj.get("uuid"),  # event_id 생성을 위해 임시 보관(_접두 = 출력 전 제거)
        }

    def _event_id(self, base: dict[str, Any], total: int, index: int) -> str:
        uuid = base.get("_uuid") or f"noid-{base.get('session_id')}-{base.get('ts')}"
        if total <= 1:
            return str(uuid)
        return f"{uuid}#{index}"

    def _finalize(self, base: dict[str, Any], events: list[dict[str, Any]], raw: Optional[dict]) -> list[dict[str, Any]]:
        total = len(events)
        out = []
        for i, ev in enumerate(events):
            merged = {k: v for k, v in base.items() if not k.startswith("_")}
            merged.update(ev)
            merged["event_id"] = self._event_id(base, total, i)
            if raw is not None:
                merged["raw"] = raw
            out.append(merged)
        return out

    def _from_assistant(self, msg: dict[str, Any], base: dict[str, Any], raw: Optional[dict]) -> list[dict[str, Any]]:
        content = msg.get("content")
        blocks = content if isinstance(content, list) else [content]
        events: list[dict[str, Any]] = []
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "tool_use":
                events.append(self._tool_use_event(blk))
            elif btype == "text":
                text = blk.get("text")
                if isinstance(text, str):
                    events.append({
                        "kind": "assistant_text",
                        "payload": {"text": _redact(text) if self.redact else text},
                    })
            elif btype == "thinking":
                if self.store_thinking:
                    events.append({
                        "kind": "assistant_thinking",
                        "payload": {"text": blk.get("thinking") or blk.get("text") or ""},
                    })
            # 모르는 블록 type -> skip
        return self._finalize(base, events, raw)

    def _tool_use_event(self, blk: dict[str, Any]) -> dict[str, Any]:
        name = blk.get("name")
        tool_input = blk.get("input")
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        if name == "Bash":
            payload = {
                "command": tool_input.get("command"),
                "description": tool_input.get("description"),
                "run_in_background": tool_input.get("run_in_background", False),
            }
        else:
            payload = {"input": tool_input}
        if self.redact:
            payload = json.loads(_redact(json.dumps(payload, ensure_ascii=False)))
        return {
            "kind": "tool_use",
            "tool": name,
            "tool_use_id": blk.get("id"),
            "payload": payload,
        }

    def _from_user(self, msg: dict[str, Any], base: dict[str, Any], raw: Optional[dict]) -> list[dict[str, Any]]:
        content = msg.get("content")
        events: list[dict[str, Any]] = []

        if isinstance(content, str):
            events.append({
                "kind": "user_prompt",
                "payload": {"text": _redact(content) if self.redact else content},
            })
        elif isinstance(content, list):
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "tool_result":
                    out = _tool_result_text(blk.get("content"))
                    if self.redact:
                        out = _redact(out)
                    events.append({
                        "kind": "tool_result",
                        "tool_use_id": blk.get("tool_use_id"),
                        "is_error": bool(blk.get("is_error", False)),
                        "payload": {"output_full": out},
                    })
                elif blk.get("type") == "text":
                    text = blk.get("text")
                    if isinstance(text, str):
                        events.append({
                            "kind": "user_prompt",
                            "payload": {"text": _redact(text) if self.redact else text},
                        })
                # 모르는 블록 -> skip
        return self._finalize(base, events, raw)
