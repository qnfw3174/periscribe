"""프록시 lockout 예방(자동 직결 fail-open) E2E.

실제 ~/.claude / 레지스트리 / 설치 exe 는 건드리지 않는다: HOME·LOCALAPPDATA 를 임시폴더로 격리하고,
OS 부작용 헬퍼(_set_autostart/_start_guardian/_start_collector)만 no-op 으로 스텁한 뒤
**실제 cmd_proxy_setup / cmd_guardian_run 코드 경로**를 돌린다. 프록시는 진짜로 띄웠다/내렸다 한다.

검증 시나리오(plan D):
  1. 프록시가 serve 못 하면 proxy-setup 이 env 를 안 쓰고 실패(exit 3)
  2. 프록시가 serve 하면 proxy-setup 이 검증 후 env 기록(exit 0)
  3. guardian: 프록시가 grace 넘게 죽으면 env 자동 제거(직결 fail-open)
  4. guardian: 프록시 복구되면 env 자동 재투입 / api_log_enabled=false 면 종료
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest


def _isolate(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    appdata = tmp_path / "appdata"
    home.mkdir(parents=True, exist_ok=True)
    appdata.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))


def _start_proxy(port: int):
    """실서비스와 동일한 조립(_make_server)으로 TLS 프록시를 띄우고 httpd 핸들(stop 가능)을 돌려준다."""
    from periscribe import apiproxy, proxycert, proxyguard
    certs = proxycert.ensure_certs(proxyguard.data_dir())
    httpd = apiproxy._make_server("testhost", port, str(proxyguard.data_dir() / "spool.jsonl"),
                                  str(proxyguard.data_dir() / "policy.json"),
                                  certs["server_pem"], certs["server_key"])
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, certs


def _wait(cond, timeout: float, interval: float = 0.1) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(interval)
    return cond()


def _write_config(path: Path, **kw) -> None:
    base = {"api_log_enabled": True, "api_proxy_port": kw.get("api_proxy_port", 8097),
            "machine_id": "testhost", "watch_dir": str(path.parent / "watch"),
            "log_file": "", "heartbeat_interval": 30, "ingest_url": "x", "device_token": "x"}
    base.update(kw)
    path.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def stub_os_side_effects(monkeypatch):
    """레지스트리/프로세스 spawn 헬퍼를 no-op 으로(테스트가 실제 자동시작·프로세스를 만들지 않게)."""
    from periscribe import __main__ as m
    monkeypatch.setattr(m, "_set_autostart", lambda *a, **k: None)
    monkeypatch.setattr(m, "_del_autostart", lambda *a, **k: None)
    monkeypatch.setattr(m, "_start_guardian", lambda *a, **k: None)
    monkeypatch.setattr(m, "_start_collector", lambda *a, **k: None)
    return m


def test_setup_refuses_when_proxy_dead(tmp_path, monkeypatch, stub_os_side_effects):
    """시나리오 1: 프록시가 없으면 proxy-setup 이 env 를 안 쓰고 exit 3."""
    _isolate(monkeypatch, tmp_path)
    from periscribe import proxyguard
    m = stub_os_side_effects
    monkeypatch.setattr(proxyguard, "SETUP_WAIT_S", 1.5)  # 빨리 실패하도록
    cfg = tmp_path / "config.json"
    _write_config(cfg, api_proxy_port=8097)

    rc = m.cmd_proxy_setup(["--config", str(cfg), "--port", "8097"])

    assert rc == 3
    assert proxyguard.env_has_proxy() is False          # env 안 씀 → Claude 직결 정상
    # 의도는 켜둔 상태(나중에 프록시 뜨면 guardian 이 자동 켜도록)
    assert json.loads(cfg.read_text(encoding="utf-8"))["api_log_enabled"] is True


def test_setup_writes_env_when_proxy_healthy(tmp_path, monkeypatch, stub_os_side_effects):
    """시나리오 2: 프록시가 serve 하면 proxy-setup 이 검증 후 env 기록(exit 0)."""
    _isolate(monkeypatch, tmp_path)
    from periscribe import proxyguard
    m = stub_os_side_effects
    httpd, _ = _start_proxy(8098)
    try:
        cfg = tmp_path / "config.json"
        _write_config(cfg, api_proxy_port=8098)
        rc = m.cmd_proxy_setup(["--config", str(cfg), "--port", "8098"])
        assert rc == 0
        assert proxyguard.env_has_proxy() is True
        env = json.loads(proxyguard.settings_json_path().read_text(encoding="utf-8"))["env"]
        assert env["ANTHROPIC_BASE_URL"] == "https://127.0.0.1:8098"
        assert env["NODE_EXTRA_CA_CERTS"].endswith("ca.pem")
    finally:
        httpd.shutdown()


def test_guardian_strips_then_readds(tmp_path, monkeypatch, stub_os_side_effects):
    """시나리오 3+4: guardian 이 프록시 죽으면 env 제거(직결), 복구되면 재투입, 의도 off 면 종료."""
    _isolate(monkeypatch, tmp_path)
    from periscribe import proxyguard
    m = stub_os_side_effects
    # 빠른 테스트용으로 grace/주기 축소
    monkeypatch.setattr(proxyguard, "DOWN_GRACE_S", 1.0)
    monkeypatch.setattr(proxyguard, "UP_STABLE_S", 0.5)
    monkeypatch.setattr(proxyguard, "GUARDIAN_TICK_S", 0.3)

    PORT = 8099
    cfg = tmp_path / "config.json"
    _write_config(cfg, api_proxy_port=PORT)

    # 초기 상태: 프록시는 죽어 있고 env 는 박혀 있음(=lockout 위험 상황)
    from periscribe import proxycert
    certs = proxycert.ensure_certs(proxyguard.data_dir())
    proxyguard.merge_settings_env({"ANTHROPIC_BASE_URL": f"https://127.0.0.1:{PORT}",
                                   "NODE_EXTRA_CA_CERTS": certs["ca_pem"]})
    assert proxyguard.env_has_proxy() is True

    # guardian 기동
    t = threading.Thread(target=m.cmd_guardian_run, args=(["-c", str(cfg)],), daemon=True)
    t.start()

    # 3) 프록시가 계속 죽어 있으므로 grace(1s) 넘기면 직결 URL 로 자동 덮어씀 → Claude 직결
    assert _wait(lambda: proxyguard.env_has_proxy() is False, timeout=6.0), "env가 직결로 전환되지 않음"
    assert proxyguard.read_status().get("last_action") == "stripped"
    # 키는 **남아 있고** 값이 직결이어야 한다 — 키 삭제는 실행 중 세션의 병합 env 에 반영되지 않아
    # 죽은 프록시를 계속 바라보는 lockout 이 되기 때문(덮어쓰기만이 핫리로드됨).
    env = json.loads(proxyguard.settings_json_path().read_text(encoding="utf-8"))["env"]
    assert env["ANTHROPIC_BASE_URL"] == proxyguard.DIRECT_BASE_URL
    # 상주 CA(NODE_EXTRA_CA_CERTS)는 유지돼야 한다 — 빼면 다음 ON 때 떠 있는 세션이 TLS 불신뢰로 끊김
    assert proxyguard.env_has_ca() is True, "fail-open이 상주 CA까지 제거함"

    # 4a) 프록시 복구 → UP_STABLE 넘기면 env 자동 재투입
    httpd, _ = _start_proxy(PORT)
    try:
        assert _wait(lambda: proxyguard.env_has_proxy() is True, timeout=6.0), "env가 자동 재투입되지 않음"
        assert proxyguard.read_status().get("last_action") == "readded"
    finally:
        httpd.shutdown()

    # 4b) 의도 off(teardown 모사) → guardian 자가종료
    proxyguard.strip_proxy_env()
    data = json.loads(cfg.read_text(encoding="utf-8"))
    data["api_log_enabled"] = False
    cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    t.join(timeout=5.0)
    assert not t.is_alive(), "api_log_enabled=false 인데 guardian 이 종료되지 않음"


def test_strip_keeps_resident_ca_unless_full(tmp_path, monkeypatch):
    """strip 기본은 BASE_URL 을 직결값으로 덮어쓰기(상주 CA 유지·타 env 보존), include_ca=True 면 CA 제거.
    어느 경로든 BASE_URL 키 자체는 남는다(키 삭제는 실행 중 세션에 미반영 — lockout 방지)."""
    _isolate(monkeypatch, tmp_path)
    from periscribe import proxyguard
    proxyguard.merge_settings_env({"ANTHROPIC_BASE_URL": "https://127.0.0.1:8097",
                                   "NODE_EXTRA_CA_CERTS": "C:/x/ca.pem"})
    # 사용자가 직접 넣은 무관한 env 키는 어떤 strip 에서도 살아남아야 한다
    proxyguard.merge_settings_env({"FOO": "bar"})

    proxyguard.strip_proxy_env()
    assert proxyguard.env_has_proxy() is False
    assert proxyguard.env_has_ca() is True
    env = json.loads(proxyguard.settings_json_path().read_text(encoding="utf-8"))["env"]
    assert env["FOO"] == "bar"
    assert env["ANTHROPIC_BASE_URL"] == proxyguard.DIRECT_BASE_URL   # 키 존재 + 직결값

    proxyguard.strip_proxy_env(include_ca=True)   # uninstall 경로
    assert proxyguard.env_has_ca() is False
    env = json.loads(proxyguard.settings_json_path().read_text(encoding="utf-8"))["env"]
    assert env == {"FOO": "bar", "ANTHROPIC_BASE_URL": proxyguard.DIRECT_BASE_URL}


def test_env_has_proxy_value_semantics(tmp_path, monkeypatch):
    """env_has_proxy 는 '키 존재'가 아니라 '값이 우리 프록시'로 판정한다."""
    _isolate(monkeypatch, tmp_path)
    from periscribe import proxyguard
    assert proxyguard.env_has_proxy() is False                       # 키 없음
    proxyguard.merge_settings_env({"ANTHROPIC_BASE_URL": proxyguard.DIRECT_BASE_URL})
    assert proxyguard.env_has_proxy() is False                       # 직결값
    proxyguard.merge_settings_env({"ANTHROPIC_BASE_URL": "https://corp-gw.example/v1"})
    assert proxyguard.env_has_proxy() is False                       # 사내 게이트웨이
    proxyguard.merge_settings_env({"ANTHROPIC_BASE_URL": "https://127.0.0.1:8097"})
    assert proxyguard.env_has_proxy() is True                        # 우리 프록시


def test_route_to_proxy_does_not_save_direct_url_as_orig(tmp_path, monkeypatch):
    """OFF 가 남긴 직결 기본값을 ON 이 '사용자 게이트웨이'로 오인해 보관하면 안 된다."""
    _isolate(monkeypatch, tmp_path)
    from periscribe import proxyguard
    proxyguard.merge_settings_env({"ANTHROPIC_BASE_URL": proxyguard.DIRECT_BASE_URL})
    saved = proxyguard.route_to_proxy("https://127.0.0.1:8097", "C:/x/ca.pem")
    assert saved is None
    assert not (proxyguard.data_dir() / "proxy-orig-env.json").is_file()


def test_strip_noop_when_key_absent_or_foreign(tmp_path, monkeypatch):
    """키가 없으면 strip 이 키를 추가하지 않고, 우리 것이 아닌 값(사용자 게이트웨이)은 건드리지 않는다."""
    _isolate(monkeypatch, tmp_path)
    from periscribe import proxyguard
    # 키 없음 → no-op(설정 파일에 키 추가 안 함)
    proxyguard.strip_proxy_env()
    data = json.loads(proxyguard.settings_json_path().read_text(encoding="utf-8")) \
        if proxyguard.settings_json_path().is_file() else {}
    assert "ANTHROPIC_BASE_URL" not in (data.get("env") or {})
    # 사용자 게이트웨이 값 → 불변(guardian fail-open 경로도 env_has_proxy=False 라 여기까지 안 옴)
    proxyguard.merge_settings_env({"ANTHROPIC_BASE_URL": "https://corp-gw.example/v1"})
    proxyguard.strip_proxy_env()
    env = json.loads(proxyguard.settings_json_path().read_text(encoding="utf-8"))["env"]
    assert env["ANTHROPIC_BASE_URL"] == "https://corp-gw.example/v1"


def test_enable_saves_and_disable_restores_orig(tmp_path, monkeypatch, stub_os_side_effects):
    """ON 이 기존 사용자 ANTHROPIC_BASE_URL 을 보관하고 OFF 가 복원한다(orig 파일 생성/삭제 포함)."""
    _isolate(monkeypatch, tmp_path)
    from periscribe import proxyguard
    m = stub_os_side_effects
    proxyguard.merge_settings_env({"ANTHROPIC_BASE_URL": "https://corp-gw.example/v1"})

    PORT = 8100
    httpd, _ = _start_proxy(PORT)
    try:
        cfg = tmp_path / "config.json"
        _write_config(cfg, api_proxy_port=PORT)
        ok, _lines = m._proxy_enable(cfg, PORT)
        assert ok
        env = json.loads(proxyguard.settings_json_path().read_text(encoding="utf-8"))["env"]
        assert env["ANTHROPIC_BASE_URL"] == f"https://127.0.0.1:{PORT}"
        assert (proxyguard.data_dir() / "proxy-orig-env.json").is_file()

        m._proxy_disable(cfg)
        env = json.loads(proxyguard.settings_json_path().read_text(encoding="utf-8"))["env"]
        assert env["ANTHROPIC_BASE_URL"] == "https://corp-gw.example/v1"   # 복원
        assert not (proxyguard.data_dir() / "proxy-orig-env.json").is_file()
    finally:
        httpd.shutdown()
