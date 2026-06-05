"""Sink — emit(events) 한 군데로 출력 추상화.

기본 구현은 Edge Function(ingest) 적재. 각 PC는 service_role/anon 없이 device_token만 보유한다.
함수가 토큰을 검증해 owner_id/device_id를 스탬프하고 insert한다(멱등). 표준 라이브러리 urllib만 사용.
실패 시 예외를 던져 호출자가 오프셋을 전진시키지 않게 한다(store-and-forward, spec §3.3).
"""

from __future__ import annotations

import json
import platform
import urllib.error
import urllib.request
from typing import Any, Protocol


class Sink(Protocol):
    def emit(self, events: list[dict[str, Any]]) -> None:
        """이벤트 배치를 적재한다. 실패 시 예외를 던진다(부분 성공으로 처리하지 않음)."""
        ...


class SinkError(Exception):
    pass


class IngestSink:
    """Edge Function(ingest)로 디바이스 토큰 인증하여 적재. service_role 불필요."""

    def __init__(self, ingest_url: str, device_token: str, machine_id: str = "",
                 collector_version: str = "", timeout: float = 30.0) -> None:
        self.url = ingest_url
        self.token = device_token
        self.timeout = timeout
        self.machine = {
            # devices.machine_id 가 events.machine_id(설정값)와 일치하도록 동일 값 사용.
            "hostname": machine_id or platform.node(),
            "platform": f"{platform.system()} {platform.release()}",
            "version": collector_version,
            "machine_id": machine_id,
        }

    def emit(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        # 키 정규화(PGRST102 방지) + NUL 제거(22P05 방지)는 함수가 아니라 여기서.
        rows = _strip_nul(_normalize_rows(events))
        self._post(rows)

    def beat(self) -> None:
        """유휴 하트비트: 빈 events로 호출 → 함수가 devices.last_seen 갱신."""
        self._post([])

    def _post(self, rows: list[dict[str, Any]]) -> None:
        body = json.dumps(
            {"device_token": self.token, "machine": self.machine, "events": rows},
            ensure_ascii=False, default=str,
        ).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status not in (200, 201, 204):
                    raise SinkError(f"ingest HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise SinkError(f"ingest HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            # 네트워크 실패 -> 재시도 대상(오프셋 전진 금지)
            raise SinkError(f"ingest 연결 실패: {e.reason}") from e


def _strip_nul(obj: Any) -> Any:
    """문자열 값에서 NUL 문자를 재귀적으로 제거(Postgres text/jsonb 미지원, 22P05)."""
    if isinstance(obj, str):
        return obj.replace("\x00", "") if "\x00" in obj else obj
    if isinstance(obj, dict):
        return {k: _strip_nul(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_nul(v) for v in obj]
    return obj


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
