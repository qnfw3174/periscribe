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
import ssl
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
    """실제 apiproxy._Handler 로 TLS 프록시를 띄우고 httpd 핸들(stop 가능)을 돌려준다."""
    from periscribe import apiproxy, proxycert, proxyguard
    certs = proxycert.ensure_certs(proxyguard.data_dir())
    httpd = apiproxy.ThreadingHTTPServer(("127.0.0.1", port), apiproxy._Handler)
    httpd.ctx = apiproxy._Ctx("testhost", str(proxyguard.data_dir() / "spool.jsonl"),
                              str(proxyguard.data_dir() / "policy.json"), None)
    sslctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sslctx.load_cert_chain(certfile=certs["server_pem"], keyfile=certs["server_key"])
    httpd.socket = sslctx.wrap_socket(httpd.socket, server_side=True)
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

    # 3) 프록시가 계속 죽어 있으므로 grace(1s) 넘기면 env 자동 제거 → Claude 직결
    assert _wait(lambda: proxyguard.env_has_proxy() is False, timeout=6.0), "env가 자동 제거되지 않음"
    assert proxyguard.read_status().get("last_action") == "stripped"

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
