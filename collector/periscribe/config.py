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

    # 컨테이너(devcontainer) transcript 루트. 비우면 컨테이너 기능 off.
    # 하위 첫 폴더명이 container_id (예: <root>/<container_id>/<proj>/<session>.jsonl).
    container_root: str = ""

    # 식별
    machine_id: str = field(default_factory=socket.gethostname)
    source: str = "claude-code"

    # 적재 대상 — Edge Function(ingest) 엔드포인트 + 디바이스 토큰.
    # service_role/anon 키는 더 이상 보유하지 않는다(토큰 유출돼도 이 머신 insert만 가능).
    ingest_url: str = ""             # 예: https://xxxx.supabase.co/functions/v1/ingest
    device_token: str = ""           # 웹에서 발급받은 머신 등록 토큰. 로컬에만!
    batch_size: int = 500            # 한 번에 보낼 최대 이벤트 수

    # 오프셋 영속
    checkpoint_path: str = "checkpoints/offsets.json"

    # 시작 동작
    backfill: int = 0                # 기존 파일에서 마지막 N줄 백필(0이면 EOF부터)

    # 옵션
    store_raw: bool = False          # 원본 라인을 events.raw 에 저장할지
    store_thinking: bool = False     # thinking 블록 저장 여부(기본 무시)
    redact: bool = False             # 수집 단계 민감정보 마스킹(토큰/키/비번 패턴)

    # E2EE(payload 암호화). encrypt=true면 owner 공개키를 받아 per-device DEK를 만들어
    # payload/raw 를 암호화 적재한다. dek는 부트스트랩 후 자동 기록(수동 입력 X). crypto.py 참고.
    encrypt: bool = True             # 암호화 적재 사용(공개키 받기 전엔 적재 보류)
    dek: str = ""                    # per-device DEK(base64). 비면 공개키 수신 후 자동 생성
    dek_kid: int = 1                 # 사용한 owner 공개키 세대

    # 헬스(하트비트) — 유휴 시 빈 ingest 호출로 last_seen 유지. 0이면 비활성.
    heartbeat_interval: float = 30.0
    # 파일 로그(서비스/무콘솔 모드 진단용). 비우면 stderr만. 크기 기반 로테이션.
    log_file: str = ""
    log_max_bytes: int = 5_000_000   # 로테이션 임계(파일당)
    log_backups: int = 3

    # 로드한 config.json 경로(DEK 부트스트랩 후 되쓰기용). 직렬화 대상 아님.
    source_path: str = ""

    # ---- 로드 ----
    @classmethod
    def load(cls, path: Optional[str] = None) -> "Config":
        data: dict[str, Any] = {}
        cfg_path = Path(path) if path else Path("config.json")
        if cfg_path.is_file():
            # utf-8-sig: PowerShell/에디터가 붙인 UTF-8 BOM이 있어도 안전하게 처리.
            data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))

        cfg = cls()
        cfg.source_path = str(cfg_path)
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
        if not self.ingest_url:
            missing.append("ingest_url")
        if not self.device_token:
            missing.append("device_token")
        if missing:
            raise ValueError(
                "필수 설정 누락: " + ", ".join(missing)
                + " (config.json 또는 PERISCRIBE_* 환경변수로 지정)"
            )

    def persist_dek(self, dek_b64: str, kid: int) -> None:
        """부트스트랩으로 생성한 per-device DEK를 config.json에 되쓴다(재시작 시 재사용).
        파일이 없거나 쓰기 실패해도 메모리값(self.dek)은 유효하므로 조용히 넘어간다."""
        self.dek = dek_b64
        self.dek_kid = kid
        p = Path(self.source_path) if self.source_path else None
        if not p or not p.is_file():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))
            data["dek"] = dek_b64
            data["dek_kid"] = kid
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass


def _coerce(value: str, to_type: type) -> Any:
    if to_type is bool:
        return value.strip().lower() in ("1", "true", "yes", "on")
    if to_type is int:
        return int(value)
    if to_type is float:
        return float(value)
    return value
