"""proxyguard — Claude API 프록시 lockout 방지(자동 직결 fail-open)의 공유 primitive.

핵심 원칙: **의도(config.api_log_enabled)와 라이브 상태(settings.json env)를 분리**한다.
- api_log_enabled = "로깅을 원함". proxy-setup이 set, proxy-teardown만 clear. 컬렉터는 이것만 보고
  프록시를 계속 되살린다.
- settings.json env(ANTHROPIC_BASE_URL/NODE_EXTRA_CA_CERTS) = "지금 Claude를 프록시로 라우팅".
  guardian이 프록시 건강 상태에 따라 자동으로 넣고/뺀다. 프록시가 일정 시간 못 살아나면 env를 빼서
  Claude를 Anthropic 직결로 돌린다(로깅만 잠깐 멈춤) → Claude는 절대 lockout 되지 않는다.

표준 라이브러리만. 프록시(proxy-run)·컬렉터·guardian·CLI 어디서든 import 가능(순환 없음)."""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

# ---- 공유 상수 ----
HEALTH_PATH = "/__periscribe_health"   # 프록시가 업스트림 미전달로 200 "ok" 응답하는 로컬 헬스 라우트
PROBE_TIMEOUT_S = 2.0                   # 헬스 프로브 1회 타임아웃
SETUP_WAIT_S = 15.0                     # proxy-setup이 프록시 기동을 기다리는 최대 시간
GUARDIAN_TICK_S = 15.0                  # guardian 점검 주기
DOWN_GRACE_S = 60.0                     # 프록시가 이만큼 비정상이면 env 제거(직결)
UP_STABLE_S = 10.0                      # 프록시가 이만큼 정상이면 env 재투입(비대칭 히스테리시스 → flapping 방지)

_LOCK_NAME = "settings.lock"
_STATUS_NAME = "proxy-failsafe.json"
_ENV_KEYS = ("ANTHROPIC_BASE_URL", "NODE_EXTRA_CA_CERTS")


# ---- 경로(자체 보유: 순환 import 회피. __main__._data_dir/_settings_json_path 와 동일 규칙) ----
def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Periscribe"


def settings_json_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


# ---- 헬스 점검 ----
def port_alive(port: int) -> bool:
    """127.0.0.1:port 에 누가 listen 중인지 0.5s TCP connect 로 확인(collector._port_alive 와 동일)."""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def health_probe(port: int, ca_pem: str, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """우리 CA로 TLS 검증하며 GET /__periscribe_health 를 쳐서 200 "ok" 인지 확인.
    소켓 + (우리 인증서로) TLS 핸드셰이크 + 핸들러 생존을 한 번에 증명한다. 업스트림은 일부러 안 본다
    (업스트림 장애는 fail-open 502라 Claude를 brick 하지 않으므로 failsafe 트리거 대상이 아님)."""
    conn = None
    try:
        ctx = ssl.create_default_context(cafile=ca_pem)
        conn = http.client.HTTPSConnection("127.0.0.1", int(port), timeout=timeout, context=ctx)
        conn.request("GET", HEALTH_PATH)
        resp = conn.getresponse()
        body = resp.read(16)
        return resp.status == 200 and body.strip() == b"ok"
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ---- settings.json env 읽기/쓰기(자체 락으로 원자화) ----
@contextmanager
def settings_lock(timeout: float = 2.0) -> Iterator[None]:
    """guardian·컬렉터·CLI 가 settings.json 을 동시에 못 건드리게 하는 베스트에포트 파일락.
    획득 실패(타임아웃)해도 진행한다 — 모든 쓰기가 idempotent 한 read-modify-write 라 최악도 last-write-wins
    (파일 파손 없음). 죽은 프로세스가 남긴 stale 락(10s 초과)은 깨고 진행."""
    lock = data_dir() / _LOCK_NAME
    try:
        data_dir().mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    fd: Optional[int] = None
    start = time.time()
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > 10.0:
                    os.unlink(lock)
                    continue
            except OSError:
                pass
            if time.time() - start > timeout:
                fd = None
                break
            time.sleep(0.05)
        except OSError:
            fd = None
            break
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(lock)
            except OSError:
                pass


def _read_settings() -> dict[str, Any]:
    p = settings_json_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig")) if p.is_file() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_settings(data: dict[str, Any]) -> None:
    p = settings_json_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def env_has_proxy() -> bool:
    """settings.json env 에 ANTHROPIC_BASE_URL 이 박혀 있는가(=Claude가 프록시로 라우팅 중)."""
    env = _read_settings().get("env")
    return isinstance(env, dict) and bool(env.get("ANTHROPIC_BASE_URL"))


def merge_settings_env(env_updates: dict[str, str]) -> None:
    """settings.json env 에 키들을 병합(다른 키 보존). 자체 락으로 원자화."""
    with settings_lock():
        data = _read_settings()
        env = data.get("env")
        if not isinstance(env, dict):
            env = {}
        env.update(env_updates)
        data["env"] = env
        _write_settings(data)


def strip_proxy_env() -> None:
    """settings.json env 에서 프록시 키(ANTHROPIC_BASE_URL/NODE_EXTRA_CA_CERTS)만 제거.
    api_log_enabled(의도)는 건드리지 않는다. idempotent. 자체 락으로 원자화."""
    with settings_lock():
        data = _read_settings()
        env = data.get("env")
        if not isinstance(env, dict):
            return
        changed = False
        for k in _ENV_KEYS:
            if k in env:
                env.pop(k, None)
                changed = True
        if not changed:
            return
        if env:
            data["env"] = env
        else:
            data.pop("env", None)
        _write_settings(data)


# ---- 상태 파일(관측성: "왜 로깅이 멈췄나") ----
def write_status(status: dict[str, Any]) -> None:
    p = data_dir() / _STATUS_NAME
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def read_status() -> dict[str, Any]:
    p = data_dir() / _STATUS_NAME
    try:
        return json.loads(p.read_text(encoding="utf-8-sig")) if p.is_file() else {}
    except Exception:
        return {}
