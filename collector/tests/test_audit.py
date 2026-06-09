"""OS exec 감사(audit_win) — Claude 프로세스 트리 추적 + 파서 패스스루 테스트. Sysmon/Windows 불필요."""
import json

from periscribe.audit_win import WinExecAudit
from periscribe.parser import Parser

_NS = "http://schemas.microsoft.com/win/2004/08/events/event"


def _evt(rid, eid, **data):
    ds = "".join(f"<Data Name='{k}'>{v}</Data>" for k, v in data.items())
    return (f"<Event xmlns='{_NS}'><System><EventID>{eid}</EventID>"
            f"<EventRecordID>{rid}</EventRecordID></System><EventData>{ds}</EventData></Event>")


def _doc(*evts):
    return "<Events>" + "".join(evts) + "</Events>"


def _audit(tmp_path, **kw):
    return WinExecAudit("PC", str(tmp_path / "s.jsonl"), str(tmp_path / "c.json"), **kw)


def test_subtree_only_claude(tmp_path):
    a = _audit(tmp_path)
    xml = _doc(
        _evt(40, 1, ProcessGuid="{C}", ParentProcessGuid="{X}", UtcTime="2026-06-09 01:00:00.000",
             Image="C:\\Users\\ss\\AppData\\Roaming\\npm\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe",
             CommandLine="claude", CurrentDirectory="C:\\repo\\", User="PC\\ss"),
        _evt(41, 1, ProcessGuid="{P}", ParentProcessGuid="{C}",
             Image="C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", CommandLine="powershell -c whoami"),
        _evt(42, 1, ProcessGuid="{G}", ParentProcessGuid="{P}",
             Image="C:\\Program Files\\Git\\cmd\\git.exe", CommandLine="git status"),   # 손자
        _evt(43, 1, ProcessGuid="{S}", ParentProcessGuid="{SVC}",
             Image="C:\\Windows\\System32\\svchost.exe", CommandLine="svchost -k netsvcs"),  # 무관
    )
    events, max_rid = a.parse_events(xml)
    assert max_rid == 43
    assert {e["event_id"] for e in events} == {"winexec-PC-40", "winexec-PC-41", "winexec-PC-42"}  # svchost 제외
    # 한 Claude 실행(루트) = 한 세션으로 묶임
    assert {e["session_id"] for e in events} == {"osexec-PC-{C}"}
    root = next(e for e in events if e["event_id"] == "winexec-PC-40")
    assert root["source"] == "os-exec" and root["kind"] == "process_exec"
    gchild = next(e for e in events if e["event_id"] == "winexec-PC-42")
    assert gchild["payload"]["command_line"] == "git status"


def test_terminate_prunes_subtree(tmp_path):
    a = _audit(tmp_path)
    a.parse_events(_doc(
        _evt(10, 1, ProcessGuid="{C}", ParentProcessGuid="{X}", Image="C:\\x\\claude.exe", CommandLine="claude"),
        _evt(11, 1, ProcessGuid="{P}", ParentProcessGuid="{C}", Image="C:\\x\\powershell.exe", CommandLine="ps"),
    ))
    assert "{P}" in a._tracked
    a.parse_events(_doc(_evt(12, 5, ProcessGuid="{P}")))   # P 종료 → 추적 제거
    assert "{P}" not in a._tracked
    # P 사라진 뒤 P의 자식은 더 이상 Claude 서브트리가 아님
    events, _ = a.parse_events(_doc(
        _evt(13, 1, ProcessGuid="{Q}", ParentProcessGuid="{P}", Image="C:\\x\\cmd.exe", CommandLine="x")))
    assert events == []


def test_no_root_no_capture(tmp_path):
    # 루트(claude) 없이 일반 프로세스만 → 아무것도 안 잡음(사람 쉘 제외)
    a = _audit(tmp_path)
    events, _ = a.parse_events(_doc(
        _evt(1, 1, ProcessGuid="{A}", ParentProcessGuid="{B}", Image="C:\\x\\powershell.exe", CommandLine="ls"),
        _evt(2, 1, ProcessGuid="{C2}", ParentProcessGuid="{A}", Image="C:\\x\\git.exe", CommandLine="git log"),
    ))
    assert events == []


def test_deny_image_in_subtree(tmp_path):
    a = _audit(tmp_path, deny_images=["git.exe"])
    events, _ = a.parse_events(_doc(
        _evt(20, 1, ProcessGuid="{C}", ParentProcessGuid="{X}", Image="C:\\x\\claude.exe", CommandLine="claude"),
        _evt(21, 1, ProcessGuid="{G}", ParentProcessGuid="{C}", Image="C:\\x\\git.exe", CommandLine="git status"),
    ))
    assert {e["event_id"] for e in events} == {"winexec-PC-20"}   # git.exe deny


def test_parse_events_defensive(tmp_path):
    a = _audit(tmp_path)
    assert a.parse_events("not xml") == ([], 0)
    assert a.parse_events("") == ([], 0)


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
    assert p.parse_line(json.dumps({"_periscribe_event": 1, "kind": "x"})) == []  # 필수 누락 → skip


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
    spool.write_bytes(b"")  # 최초 실행 시 EOF → 이후 append분만 수집
    cfg = Config()
    cfg.watch_dir = str(tmp_path)
    cfg.checkpoint_path = str(tmp_path / "cp.json")
    cfg.encrypt = False
    cfg.redact = False
    c = Collector(cfg, _CapSink())
    sink = c.sink
    c._process_file(str(spool), first_run=True)
    line = json.dumps({"_periscribe_event": 1, "event_id": "winexec-PC-7", "source": "os-exec",
                       "kind": "process_exec", "session_id": "osexec-PC-1",
                       "ts": "2026-06-09T00:00:00Z", "payload": {"command_line": "git status"}})
    with open(spool, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    c._process_file(str(spool), first_run=False)
    assert len(sink.events) == 1
    ev = sink.events[0]
    assert ev["source"] == "os-exec" and ev["event_id"] == "winexec-PC-7"
    assert ev["payload"]["command_line"] == "git status"
    assert "_periscribe_event" not in ev
