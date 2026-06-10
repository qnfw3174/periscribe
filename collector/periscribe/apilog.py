"""apilog — Anthropic Messages API 요청/응답을 정규화 이벤트(dict)로 변환.

transcript와 동일한 kind(user_prompt/assistant_text/tool_use/tool_result)로 매핑해 웹 렌더를 그대로 재사용한다.
세션은 대화 지문(system+messages[0] 해시)으로 묶고, event_id 는 멱등(message.id / 턴 해시).
프록시(apiproxy)가 이 함수들로 이벤트를 만들어 spool 에 append → 기존 파이프라인 수집.
표준 라이브러리만.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from typing import Any, Optional


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sha(s: str, n: int = 16) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:n]


def _dumps(o: Any) -> str:
    try:
        return json.dumps(o, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return str(o)


def session_id_for(body: dict[str, Any]) -> str:
    """세션 식별. Claude는 metadata.user_id(JSON 문자열)에 안정적인 session_id 를 실어 보낸다
    (한 대화의 모든 요청에서 동일) → 이걸 쓴다. 없으면 대화 지문(system+messages[0])으로 폴백."""
    md = body.get("metadata")
    if isinstance(md, dict):
        uid = md.get("user_id")
        if isinstance(uid, str) and uid:
            try:
                sid = json.loads(uid).get("session_id")
                if sid:
                    # 접두사 없이 raw session_id 사용 → transcript(같은 session_id)와 한 세션으로 합쳐져
                    # 웹에서 source 탭으로 transcript ↔ API 를 같은 대화 안에서 나눠 본다.
                    return str(sid)
            except Exception:
                pass
    system = body.get("system", "")
    msgs = body.get("messages") or []
    first = msgs[0] if msgs else {}
    return "api-" + _sha(_dumps(system) + "\x1f" + _dumps(first))


def _flatten_text(content: Any) -> str:
    """tool_result content(str 또는 블록 배열) → 평문."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                t = b.get("text")
                if isinstance(t, str):
                    out.append(t)
                elif "content" in b:
                    out.append(_flatten_text(b.get("content")))
            elif isinstance(b, str):
                out.append(b)
        return "\n".join(out)
    if content is None:
        return ""
    return _dumps(content)


def _base(machine_id: str, session_id: str, kind: str, event_id: str) -> dict[str, Any]:
    return {
        "_periscribe_event": 1,
        "event_id": event_id,
        "schema_version": 1,
        "source": "api",
        "machine_id": machine_id,
        "session_id": session_id,
        "kind": kind,
        "ts": _now_iso(),
        "payload": {},
    }


def _is_context(text: str) -> bool:
    """Claude가 user 메시지에 끼워넣는 컨텍스트(시스템 리마인더/슬래시명령 메타 등)는 '프롬프트'가 아니다."""
    t = (text or "").lstrip()
    return (t.startswith("<system-reminder>") or t.startswith("<command-message>")
            or t.startswith("<command-name>") or t.startswith("<local-command"))


def events_from_request(body: dict[str, Any], machine_id: str, session_id: str,
                        blocked: bool = False, block_reason: str = "") -> list[dict[str, Any]]:
    """요청 messages 중 '마지막 assistant 이후'(trailing) user 텍스트(프롬프트)·tool_result 만 이벤트화.

    claude는 요청마다 전체 대화를 재전송하지만, 그 이전 히스토리는 이벤트화하지 않는다 — 프록시 OFF
    기간의 턴이 ON 직후 첫 요청에서 소급 로깅되는 것을 막기 위함(OFF 기간 내용은 절대 기록 금지).
    프록시가 켜져 있는 동안의 신규 user 콘텐츠는 반드시 어떤 요청의 trailing 으로 한 번 오므로 누락 없고,
    event_id 는 내용 해시라 재시도/스트림 중단 재전송도 ingest 멱등 dedup 으로 안전.
    끼워넣은 컨텍스트 블록(_is_context)은 제외. assistant 턴은 응답에서 로깅.
    알려진 한계: ① ON 직후 첫 요청의 tool_result 는 짝 tool_use(OFF 기간)가 미기록인 고아일 수 있음(기록함)
    ② compact 직후처럼 assistant 가 없는 요청은 전체 처리라 요약문(과거 압축본)이 기록될 수 있음."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    msgs = body.get("messages") or []
    last_asst = -1
    for i, m in enumerate(msgs):
        if isinstance(m, dict) and m.get("role") == "assistant":
            last_asst = i
    for msg in msgs[last_asst + 1:]:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        blocks = [content] if isinstance(content, str) else (content if isinstance(content, list) else [])
        for blk in blocks:
            if isinstance(blk, str):
                _add_user_text(out, seen, blk, machine_id, session_id, blocked, block_reason)
            elif isinstance(blk, dict):
                bt = blk.get("type")
                if bt == "text" and isinstance(blk.get("text"), str):
                    _add_user_text(out, seen, blk["text"], machine_id, session_id, blocked, block_reason)
                elif bt == "tool_result":
                    tuid = blk.get("tool_use_id")
                    outp = _flatten_text(blk.get("content"))
                    eid = f"api-tr-{session_id}-{_sha(str(tuid) + outp)}"
                    if eid in seen:
                        continue
                    seen.add(eid)
                    ev = _base(machine_id, session_id, "tool_result", eid)
                    ev["tool_use_id"] = tuid
                    ev["is_error"] = bool(blk.get("is_error", False))
                    ev["payload"] = {"output_full": outp}
                    out.append(ev)
    if blocked and not out:
        # 차단됐는데 trailing 에 기록할 user 텍스트가 없으면(정책이 과거 메시지에 매칭 등)
        # 과거 내용 없이 '차단 사실'만 마커로 남긴다.
        eid = f"api-up-{session_id}-blk-{_sha(block_reason + str(len(msgs)))}"
        ev = _base(machine_id, session_id, "user_prompt", eid)
        ev["payload"] = {"text": "(요청 차단됨 — 내용 미기록)", "blocked": True, "block_reason": block_reason}
        ev["is_error"] = True
        out.append(ev)
    return out


def _add_user_text(out: list, seen: set, text: str, machine_id: str, session_id: str,
                   blocked: bool, block_reason: str) -> None:
    if not text.strip() or _is_context(text):
        return
    eid = f"api-up-{session_id}-{_sha(text)}"
    if eid in seen:
        return
    seen.add(eid)
    ev = _base(machine_id, session_id, "user_prompt", eid)
    ev["payload"] = {"text": text}
    if blocked:
        ev["payload"]["blocked"] = True
        ev["payload"]["block_reason"] = block_reason
        ev["is_error"] = True
    out.append(ev)


def events_from_message(msg: dict[str, Any], machine_id: str, session_id: str) -> list[dict[str, Any]]:
    """조립된 assistant 메시지({id, blocks:[...]}) → assistant_text/tool_use 이벤트."""
    mid = msg.get("id") or ("nomsg-" + _sha(_dumps(msg.get("blocks")), 12))
    out: list[dict[str, Any]] = []
    for i, blk in enumerate(msg.get("blocks") or []):
        if not isinstance(blk, dict):
            continue
        bt = blk.get("type")
        if bt == "text":
            ev = _base(machine_id, session_id, "assistant_text", f"api-{mid}#{i}")
            ev["payload"] = {"text": blk.get("text", "")}
            out.append(ev)
        elif bt == "tool_use":
            ev = _base(machine_id, session_id, "tool_use", f"api-{mid}#{i}")
            ev["tool"] = blk.get("name")
            ev["tool_use_id"] = blk.get("id")
            tin = blk.get("input") if isinstance(blk.get("input"), dict) else {}
            if blk.get("name") == "Bash":
                ev["payload"] = {"command": tin.get("command"),
                                 "description": tin.get("description"),
                                 "run_in_background": tin.get("run_in_background", False)}
            else:
                ev["payload"] = {"input": tin}
            out.append(ev)
    return out


def assemble_sse(raw: str) -> dict[str, Any]:
    """Anthropic SSE 스트림 텍스트 → {id, blocks:[{type:text,text}|{type:tool_use,id,name,input}]}."""
    message_id: Optional[str] = None
    blocks: dict[int, dict[str, Any]] = {}   # index -> block accumulator
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            d = json.loads(payload)
        except Exception:
            continue
        t = d.get("type")
        if t == "message_start":
            message_id = (d.get("message") or {}).get("id") or message_id
        elif t == "content_block_start":
            i = d.get("index", 0)
            cb = d.get("content_block") or {}
            if cb.get("type") == "tool_use":
                blocks[i] = {"type": "tool_use", "id": cb.get("id"), "name": cb.get("name"), "_json": ""}
            elif cb.get("type") == "text":
                blocks[i] = {"type": "text", "text": cb.get("text", "")}
            elif cb.get("type") == "thinking":
                blocks[i] = {"type": "thinking"}
            else:
                blocks[i] = {"type": cb.get("type") or "unknown"}
        elif t == "content_block_delta":
            i = d.get("index", 0)
            delta = d.get("delta") or {}
            b = blocks.setdefault(i, {"type": "text", "text": ""})
            if delta.get("type") == "text_delta":
                b["text"] = b.get("text", "") + (delta.get("text") or "")
            elif delta.get("type") == "input_json_delta":
                b["_json"] = b.get("_json", "") + (delta.get("partial_json") or "")
    # finalize tool_use input json
    out_blocks = []
    for i in sorted(blocks):
        b = blocks[i]
        if b.get("type") == "tool_use":
            try:
                b["input"] = json.loads(b.get("_json") or "{}")
            except Exception:
                b["input"] = {}
            b.pop("_json", None)
        if b.get("type") in ("text", "tool_use"):   # thinking/unknown skip(기본 store_thinking off)
            out_blocks.append(b)
    return {"id": message_id, "blocks": out_blocks}


def message_from_json(body: dict[str, Any]) -> dict[str, Any]:
    """비스트리밍 JSON 응답 → assemble_sse 와 동일 형태."""
    out_blocks = []
    for blk in body.get("content") or []:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") == "text":
            out_blocks.append({"type": "text", "text": blk.get("text", "")})
        elif blk.get("type") == "tool_use":
            out_blocks.append({"type": "tool_use", "id": blk.get("id"),
                               "name": blk.get("name"), "input": blk.get("input") or {}})
    return {"id": body.get("id"), "blocks": out_blocks}
