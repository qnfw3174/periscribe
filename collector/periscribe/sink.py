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
    """네트워크/일시 오류 — 재시도 대상(오프셋 전진 금지)."""
    pass


class SinkAuthError(SinkError):
    """401(revoked/invalid token) — 재시도 무의미. 호출자가 백오프/종료."""
    pass


class SinkDataError(SinkError):
    """서버가 payload(특정 행)를 거부(4xx) — poison 가능. emit이 이분탐색으로 격리."""
    pass


class IngestSink:
    """Edge Function(ingest)로 디바이스 토큰 인증하여 적재. service_role 불필요."""

    def __init__(self, ingest_url: str, device_token: str, machine_id: str = "",
                 collector_version: str = "", timeout: float = 30.0) -> None:
        self.url = ingest_url
        self.token = device_token
        self.timeout = timeout
        self.last_drop = ""   # 최근 poison(불량 행) 스킵 메시지(관측용)
        self.machine = {
            # devices.machine_id 가 events.machine_id(설정값)와 일치하도록 동일 값 사용.
            "hostname": machine_id or platform.node(),
            "platform": f"{platform.system()} {platform.release()}",
            "version": collector_version,
            "machine_id": machine_id,
            "last_error": None,
        }

    def set_last_error(self, msg: str) -> None:
        """하트비트에 실어 보낼 최근 오류(없으면 None)."""
        self.machine["last_error"] = msg or None

    def emit(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        if not events:
            return {}
        # 키 정규화(PGRST102 방지) + NUL 제거(22P05 방지)는 함수가 아니라 여기서.
        rows = _strip_nul(_normalize_rows(events))
        return self._emit_rows(rows)

    def _emit_rows(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """적재. 서버가 4xx로 거부(SinkDataError)하면 이분탐색으로 불량 행만 스킵."""
        try:
            return self._post(rows)
        except SinkDataError as e:
            if len(rows) <= 1:
                eid = rows[0].get("event_id") if rows else "?"
                self.last_drop = f"불량 이벤트 스킵(event_id={eid}): {e}"
                return {}  # poison 단건 → 버리고 진행(오프셋 전진 → 파일 정체 해소)
            mid = len(rows) // 2
            r1 = self._emit_rows(rows[:mid])
            r2 = self._emit_rows(rows[mid:])
            return r2 or r1

    def beat(self) -> dict[str, Any]:
        """유휴 하트비트: 빈 events로 호출 → 함수가 devices.last_seen 갱신 + 백필 요청 반환."""
        return self._post([])

    def _post(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
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
                # 응답(JSON)에 백필 요청 등이 실려 온다. 파싱 실패는 무시(적재는 성공).
                try:
                    raw = resp.read().decode("utf-8", "replace")
                    return json.loads(raw) if raw else {}
                except Exception:
                    return {}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code == 401:
                # revoked/invalid token — 재시도 무의미.
                raise SinkAuthError(f"ingest 401(revoked/invalid): {detail}") from e
            if 400 <= e.code < 500:
                # 서버가 payload(특정 행)를 거부 — poison 가능. emit이 이분탐색으로 격리.
                raise SinkDataError(f"ingest HTTP {e.code}: {detail}") from e
            # 5xx(일시적 서버/DB) — 재시도 대상.
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
