"""periscribe-agent 의 컨테이너+프록시 라우팅 인자(_proxy_run_args) 테스트.
사용자/Docker 개입 없이 순수 로직만 검증(호스트 config + ca.pem → docker run 인자)."""
from __future__ import annotations

import json
from pathlib import Path

from periscribe import agent


def _isolate(monkeypatch, tmp_path: Path) -> Path:
    data = tmp_path / "Periscribe"
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return data


def test_proxy_args_refuses_when_no_ca(tmp_path, monkeypatch):
    """ca.pem 없으면(프록시 서버 미실행) 빈 인자 + '서버 먼저 실행' 경고."""
    data = _isolate(monkeypatch, tmp_path)
    (data / "config.json").write_text(json.dumps(
        {"api_proxy_port": 8077, "api_proxy_bind": "0.0.0.0"}), encoding="utf-8")
    args, warns = agent._proxy_run_args()
    assert args == []
    assert any("CA" in w or "프록시 서버" in w for w in warns)


def test_proxy_args_builds_routing_when_ca_present(tmp_path, monkeypatch):
    """ca.pem 있으면 host.docker.internal 라우팅 인자 + CA 마운트 + add-host 생성."""
    data = _isolate(monkeypatch, tmp_path)
    (data / "config.json").write_text(json.dumps(
        {"api_proxy_port": 9000, "api_proxy_bind": "0.0.0.0"}), encoding="utf-8")
    (data / "ca.pem").write_text("dummy", encoding="utf-8")
    args, warns = agent._proxy_run_args()
    joined = " ".join(args)
    assert "ANTHROPIC_BASE_URL=https://host.docker.internal:9000" in joined
    assert "NODE_EXTRA_CA_CERTS=/etc/periscribe-ca.pem" in joined
    assert "host.docker.internal:host-gateway" in joined
    assert "--add-host" in args and "--mount" in args
    assert warns == []                       # bind 0.0.0.0 → 경고 없음


def test_proxy_args_warns_when_bind_not_open(tmp_path, monkeypatch):
    """bind 가 0.0.0.0 이 아니면 컨테이너가 못 닿는다는 경고."""
    data = _isolate(monkeypatch, tmp_path)
    (data / "config.json").write_text(json.dumps(
        {"api_proxy_port": 8077, "api_proxy_bind": "127.0.0.1"}), encoding="utf-8")
    (data / "ca.pem").write_text("dummy", encoding="utf-8")
    args, warns = agent._proxy_run_args()
    assert args   # 인자는 생성되되
    assert any("0.0.0.0" in w for w in warns)   # 경고 포함
