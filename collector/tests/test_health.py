"""IngestSink / 신규 config 옵션 회귀 테스트(네트워크 없이)."""

import json

from periscribe.config import Config
from periscribe.sink import IngestSink, _normalize_rows, _strip_nul


def test_ingest_sink_payload_shape():
    s = IngestSink("https://x.supabase.co/functions/v1/ingest", "tok123",
                   machine_id="PC-1", collector_version="9.9.9")
    assert s.url.endswith("/functions/v1/ingest")
    assert s.token == "tok123"
    assert s.machine["machine_id"] == "PC-1"
    assert s.machine["version"] == "9.9.9"
    assert s.machine["hostname"]   # platform.node()
    assert s.machine["platform"]   # "System release"


def test_strip_nul_and_normalize_still_work():
    rows = _strip_nul(_normalize_rows([
        {"event_id": "1", "tool": "Bash", "payload": {"t": "a" + chr(0)}},
        {"event_id": "2", "is_error": True},
    ]))
    keys = {frozenset(r.keys()) for r in rows}
    assert len(keys) == 1  # 동일 키집합
    by = {r["event_id"]: r for r in rows}
    assert by["1"]["payload"]["t"] == "a"  # NUL 제거
    assert by["1"]["is_error"] is None     # 합집합 채움


def test_config_requires_ingest_fields(tmp_path):
    cfg = Config.load(str(tmp_path / "nope.json"))
    try:
        cfg.validate()
        assert False, "ingest_url/device_token 없으면 ValueError"
    except ValueError as e:
        assert "ingest_url" in str(e) and "device_token" in str(e)


def test_config_loads_ingest_fields(tmp_path):
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "ingest_url": "https://x.supabase.co/functions/v1/ingest",
        "device_token": "tok", "heartbeat_interval": 5, "redact": True,
    }), encoding="utf-8")
    cfg = Config.load(str(p))
    cfg.validate()  # 통과해야 함
    assert cfg.ingest_url.endswith("/ingest")
    assert cfg.device_token == "tok"
    assert cfg.heartbeat_interval == 5
    assert cfg.redact is True
