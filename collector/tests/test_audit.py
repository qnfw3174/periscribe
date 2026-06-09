"""OS exec 감사(audit_win) XML 파싱 + 파서 패스스루 테스트. Sysmon/Windows 없이 순수 검증."""
import json

from periscribe.audit_win import WinExecAudit
from periscribe.parser import Parser

_NS = "http://schemas.microsoft.com/win/2004/08/events/event"
SAMPLE = (
    "<Events>"
    f"<Event xmlns='{_NS}'><System><EventID>1</EventID><EventRecordID>42</EventRecordID></System>"
    "<EventData>"
    "<Data Name='UtcTime'>2026-06-09 01:23:45.678</Data>"
    "<Data Name='Image'>C:\\Program Files\\Git\\cmd\\git.exe</Data>"
    "<Data Name='CommandLine'>git push origin main</Data>"
    "<Data Name='CurrentDirectory'>C:\\repo\\</Data>"
    "<Data Name='User'>PC\\ss</Data>"
    "<Data Name='ProcessId'>1234</Data><Data Name='ParentProcessId'>1000</Data>"
    "<Data Name='ParentImage'>C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe</Data>"
    "<Data Name='ProcessGuid'>{abc}</Data>"
    "</EventData></Event>"
    f"<Event xmlns='{_NS}'><System><EventID>1</EventID><EventRecordID>43</EventRecordID></System>"
    "<EventData>"
    "<Data Name='UtcTime'>2026-06-09 01:23:46.000</Data>"
    "<Data Name='Image'>C:\\Windows\\System32\\svchost.exe</Data>"
    "<Data Name='CommandLine'>svchost -k netsvcs</Data>"
    "<Data Name='ParentImage'>C:\\Windows\\System32\\services.exe</Data>"
    "</EventData></Event>"
    "</Events>"
)


def test_parse_events_shell_only(tmp_path):
    a = WinExecAudit("PC", str(tmp_path / "spool.jsonl"), str(tmp_path / "cur.json"))
    events, max_rid = a.parse_events(SAMPLE)
    assert max_rid == 43            # 두 이벤트의 RecordId 모두 인식(커서 전진용)
    assert len(events) == 1         # svchost(부모 services.exe, 비-shell)는 필터됨
    e = events[0]
    assert e["source"] == "os-exec" and e["kind"] == "process_exec"
    assert e["event_id"] == "winexec-PC-42"
    assert e["session_id"].startswith("osexec-PC-")
    assert e["payload"]["command_line"] == "git push origin main"
    assert e["payload"]["image"].endswith("git.exe")
    assert e["cwd"] == "C:\\repo\\"
    assert e["ts"] == "2026-06-09T01:23:45.678Z"
    assert e["_periscribe_event"] == 1


def test_parse_events_defensive(tmp_path):
    a = WinExecAudit("PC", str(tmp_path / "s.jsonl"), str(tmp_path / "c.json"))
    assert a.parse_events("not xml") == ([], 0)
    assert a.parse_events("") == ([], 0)


def test_deny_image_filtered(tmp_path):
    a = WinExecAudit("PC", str(tmp_path / "s.jsonl"), str(tmp_path / "c.json"),
                     deny_images=["git.exe"])
    events, _ = a.parse_events(SAMPLE)
    assert events == []   # git.exe deny → shell 매칭이어도 제외


def test_parser_passthrough_redacts_and_validates():
    p = Parser(machine_id="PC", redact=True)
    line = json.dumps({
        "_periscribe_event": 1, "event_id": "winexec-PC-42", "source": "os-exec",
        "kind": "process_exec", "session_id": "osexec-PC-1",
        "payload": {"command_line": "export TOKEN=abc123 && make build"},
    })
    out = p.parse_line(line)
    assert len(out) == 1
    ev = out[0]
    assert "_periscribe_event" not in ev
    assert ev["source"] == "os-exec" and ev["event_id"] == "winexec-PC-42"
    assert "[REDACTED]" in json.dumps(ev["payload"])   # 명령 속 토큰 마스킹
    # 필수 필드(event_id/kind/session_id) 누락 → 방어적으로 skip
    assert p.parse_line(json.dumps({"_periscribe_event": 1, "kind": "x"})) == []


class _CapSink:
    def __init__(self):
        self.events = []
    def emit(self, events):
        self.events.extend(events)
        return {}


def test_collector_ingests_osexec_spool(tmp_path):
    """spool 의 사전정규화 라인이 기존 discover/_process_file/파서 경로로 sink 까지 도달."""
    from periscribe.collector import Collector
    from periscribe.config import Config
    spool = tmp_path / "_osexec" / "host.jsonl"
    spool.parent.mkdir(parents=True)
    spool.write_bytes(b"")  # 최초 실행 시 EOF(빈 파일=offset 0) → 이후 append분만 수집
    cfg = Config()
    cfg.watch_dir = str(tmp_path)
    cfg.checkpoint_path = str(tmp_path / "cp.json")
    cfg.encrypt = False
    cfg.redact = False
    c = Collector(cfg, _CapSink())
    sink = c.sink
    c._process_file(str(spool), first_run=True)        # tailer 확립(EOF=0)
    line = json.dumps({"_periscribe_event": 1, "event_id": "winexec-PC-7", "source": "os-exec",
                       "kind": "process_exec", "session_id": "osexec-PC-1",
                       "ts": "2026-06-09T00:00:00Z", "payload": {"command_line": "git status"}})
    with open(spool, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    c._process_file(str(spool), first_run=False)        # append분 수집
    assert len(sink.events) == 1
    ev = sink.events[0]
    assert ev["source"] == "os-exec" and ev["kind"] == "process_exec"
    assert ev["event_id"] == "winexec-PC-7"
    assert ev["payload"]["command_line"] == "git status"
    assert "_periscribe_event" not in ev
