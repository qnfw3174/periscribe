"""proxyguard — Claude API 프록시 lockout 방지(자동 직결 fail-open)의 공유 primitive.

핵심 원칙: **의도(config.api_log_enabled)와 라이브 상태(settings.json env)를 분리**한다.
- api_log_enabled = "로깅을 원함". proxy on(_proxy_enable)이 set, proxy off(_proxy_disable)만 clear.
  컬렉터는 이것만 보고 프록시를 계속 되살린다.
- settings.json env(ANTHROPIC_BASE_URL) = "지금 Claude를 프록시로 라우팅". Claude는 settings env를
  프로세스 env에 **병합**하므로 키 추가/변경은 실행 중 세션에도 핫리로드되지만, 키 **삭제**는 이미 박힌
  값을 못 지운다 → OFF/fail-open 은 키 제거가 아니라 **직결 URL(DIRECT_BASE_URL)로 덮어쓰기**로 한다.
  guardian이 프록시 건강 상태에 따라 자동으로 우리 URL/직결 URL 을 오간다. 프록시가 일정 시간 못
  살아나면 직결로 덮어써 Claude를 Anthropic 직결로 돌린다(로깅만 잠깐 멈춤) → 절대 lockout 안 됨.
  사용자가 원래 쓰던 ANTHROPIC_BASE_URL(예: 사내 게이트웨이)은 ON 때 orig 파일에 보관, OFF 때 복원.
- NODE_EXTRA_CA_CERTS는 한 번 설치되면 **상주**한다(off/fail-open에도 제거 안 함). Node는 이 값을
  프로세스 시작 시에만 읽으므로, 같이 빼버리면 다음 ON 때 이미 떠 있는 세션이 base_url만 핫리로드해
  우리 CA를 불신뢰 → TLS 실패로 끊긴다. 완전 제거는 strip_proxy_env(include_ca=True) (uninstall 전용).

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
SETUP_WAIT_S = 15.0                     # proxy on(_proxy_enable)이 프록시 기동을 기다리는 최대 시간
GUARDIAN_TICK_S = 15.0                  # guardian 점검 주기
DOWN_GRACE_S = 60.0                     # 프록시가 이만큼 비정상이면 env 제거(직결)
UP_STABLE_S = 10.0                      # 프록시가 이만큼 정상이면 env 재투입(비대칭 히스테리시스 → flapping 방지)

_LOCK_NAME = "settings.lock"
_STATUS_NAME = "proxy-failsafe.json"
_ORIG_NAME = "proxy-orig-env.json"      # ON 직전 사용자의 ANTHROPIC_BASE_URL 보관(OFF 때 복원)
_ENV_KEYS = ("ANTHROPIC_BASE_URL", "NODE_EXTRA_CA_CERTS")
DIRECT_BASE_URL = "https://api.anthropic.com"   # apiproxy.UPSTREAM_HOST 와 동일(순환 import 회피로 자체 보유)


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


def is_our_proxy_url(v: Any) -> bool:
    """값이 우리 로컬 프록시 URL 인가. localhost 의 타사 프록시(LiteLLM 등)도 True 로 오판될 수 있으나
    어차피 우리 CA/포트와 병행 불가라 실사용 충돌 없음."""
    return isinstance(v, str) and v.startswith(("https://127.0.0.1", "https://localhost"))


def env_has_proxy() -> bool:
    """settings.json env 의 ANTHROPIC_BASE_URL 값이 '우리 프록시'인가(=Claude가 프록시로 라우팅 중).
    키 존재가 아니라 값으로 판정 — OFF 는 직결 URL 덮어쓰기라 키가 남아 있기 때문."""
    env = _read_settings().get("env")
    return isinstance(env, dict) and is_our_proxy_url(env.get("ANTHROPIC_BASE_URL"))


# ---- ON 직전 사용자 base_url 보관/복원 ----
def _orig_path() -> Path:
    return data_dir() / _ORIG_NAME


def _read_orig_base_url() -> Optional[str]:
    try:
        d = json.loads(_orig_path().read_text(encoding="utf-8"))
        v = d.get("ANTHROPIC_BASE_URL") if isinstance(d, dict) else None
        return v if isinstance(v, str) and v else None
    except Exception:
        return None


def _save_orig_base_url(v: str) -> None:
    try:
        _orig_path().parent.mkdir(parents=True, exist_ok=True)
        _orig_path().write_text(json.dumps({"ANTHROPIC_BASE_URL": v}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _clear_orig_base_url() -> None:
    try:
        _orig_path().unlink()
    except OSError:
        pass


def env_has_ca() -> bool:
    """settings.json env 에 NODE_EXTRA_CA_CERTS(상주 CA)가 있는가. 없으면 지금 떠 있는 Claude 세션은
    우리 CA 없이 시작된 것 → 프록시 ON 이 그 세션엔 적용 불가(재시작 필요) 판정에 쓴다."""
    env = _read_settings().get("env")
    return isinstance(env, dict) and bool(env.get("NODE_EXTRA_CA_CERTS"))


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


def route_to_proxy(base_url: str, ca_pem: str) -> Optional[str]:
    """Claude 를 우리 프록시로 라우팅(ON 진입점). 락 안에서 원자화:
    기존 ANTHROPIC_BASE_URL 이 있고 우리 것이 아니면 orig 파일에 보관(OFF 때 복원용) 후
    ANTHROPIC_BASE_URL + NODE_EXTRA_CA_CERTS 병합. 보관한 orig 값을 반환(없으면 None)."""
    saved: Optional[str] = None
    with settings_lock():
        data = _read_settings()
        env = data.get("env")
        if not isinstance(env, dict):
            env = {}
        cur = env.get("ANTHROPIC_BASE_URL")
        # 직결 기본값(우리가 OFF 때 덮어쓴 값)은 사용자 게이트웨이가 아니므로 보관 대상에서 제외 —
        # 안 그러면 OFF→ON 반복 시 직결 URL 을 '기존 게이트웨이'로 오인해 보관/복원 안내가 잘못 뜬다.
        if isinstance(cur, str) and cur and not is_our_proxy_url(cur) and cur != DIRECT_BASE_URL:
            _save_orig_base_url(cur)
            saved = cur
        env.update({"ANTHROPIC_BASE_URL": base_url, "NODE_EXTRA_CA_CERTS": ca_pem})
        data["env"] = env
        _write_settings(data)
    return saved


def strip_proxy_env(include_ca: bool = False) -> None:
    """Claude 라우팅을 직결로 되돌림(OFF/fail-open). ANTHROPIC_BASE_URL 을 **제거하지 않고**
    orig 보관값(없으면 DIRECT_BASE_URL)으로 **덮어쓴다** — Claude 는 settings env 를 프로세스 env 에
    병합만 하므로 키 삭제는 실행 중 세션에 반영되지 않지만 값 변경은 핫리로드된다(모듈 docstring 참고).
    키가 아예 없으면 no-op(불필요한 설정 추가 안 함). 값이 우리 프록시가 아니면(사용자가 손수 바꿈) 그대로 둠.
    NODE_EXTRA_CA_CERTS 는 기본 유지(상주 CA). include_ca=True(uninstall)면 CA 키만 제거 —
    이때도 ANTHROPIC_BASE_URL 은 직결값으로 남긴다(제거하면 실행 중 세션이 죽은 프록시 값을 영구 유지).
    api_log_enabled(의도)는 건드리지 않는다. idempotent. 자체 락으로 원자화."""
    with settings_lock():
        data = _read_settings()
        env = data.get("env")
        if not isinstance(env, dict):
            return
        changed = False
        cur = env.get("ANTHROPIC_BASE_URL")
        if isinstance(cur, str) and is_our_proxy_url(cur):
            env["ANTHROPIC_BASE_URL"] = _read_orig_base_url() or DIRECT_BASE_URL
            _clear_orig_base_url()
            changed = True
        if include_ca and "NODE_EXTRA_CA_CERTS" in env:
            env.pop("NODE_EXTRA_CA_CERTS", None)
            _clear_orig_base_url()
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
