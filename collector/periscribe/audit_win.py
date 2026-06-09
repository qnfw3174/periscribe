"""audit_win — Windows OS 레벨 프로세스 실행 감사를 **Claude 프로세스 트리로 한정**해 spool.

transcript(Claude Code 의존)와 별개로, Claude가 OS에서 실제 실행한 작업(도구가 띄운 프로세스 + 그 하위)을
OS 레벨로 robust하게 잡는다. 사람의 일반 쉘 명령은 제외 — 오직 Claude(claude.exe) 프로세스의 서브트리만.

동작: Sysmon이 전체 프로세스 생성(EID 1)/종료(EID 5)를 이벤트로그에 기록 → 이 모듈이 wevtutil로 폴링,
ProcessGuid 계보로 Claude 서브트리를 추적해 그 프로세스만 정규화 이벤트(source='os-exec', kind='process_exec')로
watch_dir/_osexec/<machine>.jsonl spool 에 append → 기존 Tailer/Checkpoint/E2EE/ingest 가 그대로 수집(재사용).

표준 라이브러리만 사용(subprocess/xml.etree/ctypes/json). XML 파싱은 방어적(미지/누락 필드 → skip).
"""

from __future__ import annotations

import json
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Optional

_NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}

# Claude 루트 프로세스 식별 패턴(image 경로/basename 또는 cmdline 에 부분일치, 소문자).
DEFAULT_ROOT_PATTERNS = ["claude.exe", "claude-code"]

_MAX_TRACKED = 5000  # GUID 계보 셋 상한(EID5 누수 대비)


def _basename_lower(path: str) -> str:
    if not path:
        return ""
    return path.replace("/", "\\").rsplit("\\", 1)[-1].lower()


class WinExecAudit:
    """Sysmon 프로세스 create/terminate 를 폴링해 Claude 서브트리만 정규화 이벤트로 spool."""

    def __init__(self, machine_id: str, spool_path: str, cursor_path: str,
                 log: str = "Microsoft-Windows-Sysmon/Operational",
                 root_patterns: Optional[list[str]] = None,
                 deny_images: Optional[list[str]] = None,
                 max_per_poll: int = 1000,
                 logger: Optional[Callable[[str], None]] = None) -> None:
        self.machine_id = machine_id or ""
        self.spool_path = Path(spool_path)
        self.cursor_path = Path(cursor_path)
        self.log = log
        self.root_patterns = [p.lower() for p in (root_patterns or DEFAULT_ROOT_PATTERNS)]
        self.deny_images = {s.lower() for s in (deny_images or [])}
        self.max_per_poll = max_per_poll
        self._log = logger or (lambda m: None)
        self.available = os.name == "nt"
        self._cursor = 0
        self._tracked: dict[str, str] = {}   # ProcessGuid -> session_id (Claude 서브트리)
        self._load_cursor()

    # ---- 커서 + 추적 셋 영속(같은 부팅 내 재시작 복원) ----
    def _load_cursor(self) -> None:
        try:
            d = json.loads(self.cursor_path.read_text(encoding="utf-8"))
            self._cursor = int(d.get("record_id", 0))
            t = d.get("tracked")
            if isinstance(t, dict):
                self._tracked = {str(k): str(v) for k, v in t.items()}
        except Exception:
            self._cursor, self._tracked = 0, {}

    def _save_cursor(self) -> None:
        try:
            self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cursor_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"record_id": self._cursor, "tracked": self._tracked}),
                           encoding="utf-8")
            os.replace(tmp, self.cursor_path)
        except OSError as e:
            self._log(f"[periscribe] audit 커서 저장 실패: {e}")

    # ---- 1회 폴 ----
    def poll(self) -> int:
        if not self.available:
            return 0
        try:
            xml_text = self._query(self._cursor)
        except Exception as e:  # noqa: BLE001
            self._log(f"[periscribe] audit 쿼리 실패: {e}")
            return 0
        if not xml_text.strip():
            self._maybe_reset_cursor()
            return 0
        events, max_rid = self.parse_events(xml_text)  # _tracked 갱신(상태)
        if events:
            try:
                self.spool_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.spool_path, "a", encoding="utf-8") as f:
                    for e in events:
                        f.write(json.dumps(e, ensure_ascii=False) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as e:
                self._log(f"[periscribe] audit spool 쓰기 실패: {e}")
                return 0  # 커서 전진 안 함 → 다음 폴 재시도(멱등)
        # 이벤트가 없어도(=Claude 서브트리 밖만 있었어도) 커서는 전진해야 재스캔 안 함.
        if max_rid > self._cursor:
            self._cursor = max_rid
        self._save_cursor()   # tracked 갱신분도 함께 영속
        return len(events)

    def _maybe_reset_cursor(self) -> None:
        try:
            xml_text = self._query(0, count=1, newest=True)
            mx = self._peek_max_rid(xml_text) if xml_text else 0
            if mx and mx < self._cursor:
                self._cursor, self._tracked = 0, {}
                self._save_cursor()
                self._log("[periscribe] audit 로그 리셋 감지 → 커서/추적 초기화")
        except Exception:
            pass

    def _query(self, after_rid: int, count: Optional[int] = None, newest: bool = False) -> str:
        q = f"*[System[(EventID=1 or EventID=5) and (EventRecordID>{after_rid})]]"
        cmd = ["wevtutil", "qe", self.log, f"/q:{q}", "/f:xml", "/e:Events",
               f"/c:{count or self.max_per_poll}", "/rd:" + ("true" if newest else "false")]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        if r.returncode != 0:
            return ""   # 로그 없음/권한 없음/Sysmon 미설치 → 조용히 빈 결과
        return r.stdout or ""

    # ---- XML → 정규화 이벤트(상태 추적; 테스트 가능) ----
    def parse_events(self, xml_text: str) -> tuple[list[dict[str, Any]], int]:
        out: list[dict[str, Any]] = []
        max_rid = 0
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return out, max_rid
        rows = []
        for ev in root.findall("e:Event", _NS):
            try:
                rid, eid, data = self._extract(ev)
            except Exception:
                continue
            rows.append((rid, eid, data))
            if rid > max_rid:
                max_rid = rid
        rows.sort(key=lambda t: t[0])   # RecordId 오름차순(부모를 자식보다 먼저 처리)
        for rid, eid, data in rows:
            if eid == 5:                                  # ProcessTerminate → 추적 셋에서 제거
                self._tracked.pop(data.get("ProcessGuid", ""), None)
                continue
            if eid != 1:
                continue
            node = self._map_create(rid, data)
            if node is not None:
                out.append(node)
        return out, max_rid

    def _extract(self, ev: ET.Element) -> tuple[int, int, dict[str, str]]:
        sysm = ev.find("e:System", _NS)
        rid = int(sysm.findtext("e:EventRecordID", default="0", namespaces=_NS)) if sysm is not None else 0
        eid = int(sysm.findtext("e:EventID", default="0", namespaces=_NS)) if sysm is not None else 0
        data = {d.get("Name"): (d.text or "") for d in ev.findall("e:EventData/e:Data", _NS)}
        return rid, eid, data

    def _peek_max_rid(self, xml_text: str) -> int:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return 0
        m = 0
        for ev in root.findall("e:Event", _NS):
            try:
                rid, _, _ = self._extract(ev)
            except Exception:
                rid = 0
            if rid > m:
                m = rid
        return m

    def _is_root(self, image: str, command_line: str) -> bool:
        hay = (image + " " + command_line).lower()
        return any(p in hay for p in self.root_patterns)

    def _map_create(self, rid: int, data: dict[str, str]) -> Optional[dict[str, Any]]:
        guid = data.get("ProcessGuid", "")
        pguid = data.get("ParentProcessGuid", "")
        image = data.get("Image", "")
        cmdline = data.get("CommandLine", "")
        if _basename_lower(image) in self.deny_images:
            return None
        # Claude 서브트리 판정: 루트(claude.exe)거나, 부모가 이미 추적 중이면 포함.
        if self._is_root(image, cmdline):
            sid = f"osexec-{self.machine_id}-{guid or rid}"
        elif pguid and pguid in self._tracked:
            sid = self._tracked[pguid]
        else:
            return None   # Claude 와 무관한 프로세스 → 제외(사람 쉘 등)
        if guid:
            self._tracked[guid] = sid
            if len(self._tracked) > _MAX_TRACKED:   # 누수 방지(EID5 놓침 대비)
                for k in list(self._tracked)[: len(self._tracked) - _MAX_TRACKED]:
                    self._tracked.pop(k, None)
        ut = data.get("UtcTime", "")
        ts = (ut.replace(" ", "T") + "Z") if ut else None
        return {
            "_periscribe_event": 1,
            "event_id": f"winexec-{self.machine_id}-{rid}",
            "schema_version": 1,
            "source": "os-exec",
            "machine_id": self.machine_id,
            "session_id": sid,
            "kind": "process_exec",
            "tool": _basename_lower(image) or None,
            "ts": ts,
            "cwd": data.get("CurrentDirectory") or None,
            "payload": {
                "image": image,
                "command_line": cmdline,
                "parent_image": data.get("ParentImage", ""),
                "parent_command_line": data.get("ParentCommandLine", ""),
                "process_guid": guid,
                "parent_process_guid": pguid,
                "pid": data.get("ProcessId", ""),
                "parent_pid": data.get("ParentProcessId", ""),
                "user": data.get("User", ""),
                "integrity": data.get("IntegrityLevel", ""),
            },
        }
