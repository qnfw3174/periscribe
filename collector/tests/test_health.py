"""HealthReporter / 신규 config 옵션 회귀 테스트(네트워크 없이)."""

import json

from periscribe.config import Config
from periscribe.health import HealthReporter


def test_health_reporter_fields():
    h = HealthReporter("https://x.supabase.co", "k", "PC-1", "claude-code", "9.9.9")
    assert h.endpoint == "https://x.supabase.co/rest/v1/machines"
    assert h.machine_id == "PC-1"
    assert h.collector_version == "9.9.9"
    assert h.hostname  # platform.node()
    assert h.platform  # "System release"
    assert h.started_at  # ISO ts


def test_config_new_defaults(tmp_path):
    cfg = Config.load(str(tmp_path / "nope.json"))
    assert cfg.heartbeat_interval == 30.0
    assert cfg.log_file == ""
    assert cfg.log_max_bytes == 5_000_000
    assert cfg.log_backups == 3


def test_config_loads_new_fields(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "supabase_url": "https://x.supabase.co", "supabase_key": "k",
        "heartbeat_interval": 5, "log_file": "logs/c.log", "redact": True,
    }), encoding="utf-8")
    cfg = Config.load(str(p))
    assert cfg.heartbeat_interval == 5
    assert cfg.log_file == "logs/c.log"
    assert cfg.redact is True
