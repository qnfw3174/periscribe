"""Sink — emit(events) 한 군데로 출력 추상화.

기본 구현은 Supabase(PostgREST) insert. 표준 라이브러리 urllib 만 사용한다.
멱등성: on_conflict=event_id + Prefer: resolution=ignore-duplicates.
실패 시 예외를 던져 호출자가 오프셋을 전진시키지 않게 한다(store-and-forward, spec §3.3).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Protocol


class Sink(Protocol):
    def emit(self, events: list[dict[str, Any]]) -> None:
        """이벤트 배치를 적재한다. 실패 시 예외를 던진다(부분 성공으로 처리하지 않음)."""
        ...


class SinkError(Exception):
    pass


class SupabaseSink:
    """PostgREST 직접 insert. supabase-py 없이 표준 라이브러리만으로 동작."""

    def __init__(self, url: str, key: str, table: str = "events", timeout: float = 30.0) -> None:
        self.endpoint = url.rstrip("/") + "/rest/v1/" + table
        self.key = key
        self.timeout = timeout

    def emit(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        # PostgREST 벌크 insert는 배열 내 모든 객체의 키 집합이 동일해야 한다(PGRST102).
        # 이벤트마다 tool/tool_use_id/is_error/raw 유무가 다르므로 키 합집합으로 정규화.
        rows = _normalize_rows(events)
        body = json.dumps(rows, ensure_ascii=False, default=str).encode("utf-8")
        # on_conflict=event_id 로 멱등 upsert(중복 무시)
        url = self.endpoint + "?on_conflict=event_id"
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("apikey", self.key)
        req.add_header("Authorization", "Bearer " + self.key)
        req.add_header("Content-Type", "application/json")
        # 중복 무시 + 응답 본문 최소화
        req.add_header("Prefer", "resolution=ignore-duplicates,return=minimal")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status not in (200, 201, 204):
                    raise SinkError(f"Supabase insert HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise SinkError(f"Supabase insert HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            # 네트워크 실패 -> 재시도 대상(오프셋 전진 금지)
            raise SinkError(f"Supabase 연결 실패: {e.reason}") from e


def _normalize_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """배치 내 모든 행이 동일한 키 집합을 갖도록 합집합으로 맞춘다(없는 키는 None)."""
    keys: set[str] = set()
    for ev in events:
        keys.update(ev.keys())
    return [{k: ev.get(k) for k in keys} for ev in events]


class StdoutSink:
    """디버깅/테스트용. 이벤트를 JSONL 로 stdout 에 출력."""

    def emit(self, events: list[dict[str, Any]]) -> None:
        for ev in events:
            print(json.dumps(ev, ensure_ascii=False, default=str))
