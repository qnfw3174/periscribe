"""설정 로드.

우선순위: 환경변수(PERISCRIBE_*) > config.json > 기본값.
표준 라이브러리만 사용(json). 민감한 쓰기 키는 로컬에만 보관한다.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


def _default_watch_dir() -> str:
    # ~/.claude/projects (Windows: %USERPROFILE%\.claude\projects)
    return str(Path.home() / ".claude" / "projects")


@dataclass
class Config:
    # 감시 대상
    watch_dir: str = field(default_factory=_default_watch_dir)
    poll_interval: float = 0.4  # 초. 0.3~0.5 권장(spec §2.2)

    # 식별
    machine_id: str = field(default_factory=socket.gethostname)
    source: str = "claude-code"

    # Supabase
    supabase_url: str = ""           # 예: https://xxxx.supabase.co
    supabase_key: str = ""           # service_role 또는 insert 전용 키. 로컬에만!
    table: str = "events"
    batch_size: int = 500            # 한 번에 insert 할 최대 이벤트 수

    # 오프셋 영속
    checkpoint_path: str = "checkpoints/offsets.json"

    # 시작 동작
    backfill: int = 0                # 기존 파일에서 마지막 N줄 백필(0이면 EOF부터)

    # 옵션
    store_raw: bool = False          # 원본 라인을 events.raw 에 저장할지
    store_thinking: bool = False     # thinking 블록 저장 여부(기본 무시)
    redact: bool = False             # 수집 단계 민감정보 마스킹(토큰/키/비번 패턴)

    # ---- 로드 ----
    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        data: dict[str, Any] = {}
        cfg_path = Path(path) if path else Path("config.json")
        if cfg_path.is_file():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))

        cfg = cls()
        for key in vars(cfg):
            if key not in data:
                continue
            val = data[key]
            if val is None:
                continue
            # 빈 문자열은 "미지정"으로 보고 기본값(hostname/기본 감시 경로 등)을 유지한다.
            if isinstance(val, str) and val.strip() == "":
                continue
            setattr(cfg, key, val)

        # 환경변수 override (PERISCRIBE_<UPPER>)
        for key in vars(cfg):
            env = os.environ.get("PERISCRIBE_" + key.upper())
            if env is None:
                continue
            cur = getattr(cfg, key)
            setattr(cfg, key, _coerce(env, type(cur)))

        return cfg

    def validate(self) -> None:
        missing = []
        if not self.supabase_url:
            missing.append("supabase_url")
        if not self.supabase_key:
            missing.append("supabase_key")
        if missing:
            raise ValueError(
                "필수 설정 누락: " + ", ".join(missing)
                + " (config.json 또는 PERISCRIBE_* 환경변수로 지정)"
            )


def _coerce(value: str, to_type: type) -> Any:
    if to_type is bool:
        return value.strip().lower() in ("1", "true", "yes", "on")
    if to_type is int:
        return int(value)
    if to_type is float:
        return float(value)
    return value
