"""HealthReporter — machines 테이블에 하트비트(upsert)를 보낸다.

sink.py의 PostgREST(urllib) 패턴을 재사용. 실패해도 수집 루프에 영향을 주지 않도록
호출부에서 try/except로 감싼다. service_role 키라 RLS를 우회해 upsert한다.
"""

from __future__ import annotations

import json
import platform
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class HealthReporter:
    def __init__(
        self,
        url: str,
        key: str,
        machine_id: str,
        source: str = "claude-code",
        collector_version: str = "",
        table: str = "machines",
        timeout: float = 15.0,
    ) -> None:
        self.endpoint = url.rstrip("/") + "/rest/v1/" + table
        self.key = key
        self.machine_id = machine_id
        self.source = source
        self.collector_version = collector_version
        self.timeout = timeout
        self.started_at = _now_iso()
        self.hostname = platform.node()
        self.platform = f"{platform.system()} {platform.release()}"

    def beat(self) -> None:
        """machines 행을 upsert. 실패 시 예외를 던지므로 호출부에서 흡수한다."""
        row = {
            "machine_id": self.machine_id,
            "hostname": self.hostname,
            "platform": self.platform,
            "source": self.source,
            "collector_version": self.collector_version,
            "started_at": self.started_at,
            "last_seen": _now_iso(),
        }
        body = json.dumps([row], ensure_ascii=False).encode("utf-8")
        url = self.endpoint + "?on_conflict=machine_id"
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("apikey", self.key)
        req.add_header("Authorization", "Bearer " + self.key)
        req.add_header("Content-Type", "application/json")
        # 이미 있으면 갱신(merge-duplicates = upsert)
        req.add_header("Prefer", "resolution=merge-duplicates,return=minimal")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            if resp.status not in (200, 201, 204):
                raise RuntimeError(f"heartbeat HTTP {resp.status}")
