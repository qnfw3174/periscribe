"""audit_win — Windows OS 레벨 프로세스 실행 감사(Sysmon EID 1) → 정규화 이벤트 spool.

transcript(Claude Code 의존)와 별개로 "쉘 작업 자체"를 잡는다. Sysmon이 프로세스 생성을
Microsoft-Windows-Sysmon/Operational 이벤트로그에 남기면, 이 모듈이 wevtutil로 폴링해
정규화 이벤트(source='os-exec', kind='process_exec')를 watch_dir/_osexec/<machine>.jsonl 에
append 한다 → 기존 Tailer/Checkpoint/E2EE/ingest 가 그대로 처리(코드 재사용).

표준 라이브러리만 사용(subprocess/xml.etree/ctypes/json). XML 파싱은 방어적(미지/누락 필드 → skip).
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Optional

# Windows 이벤트로그 XML 네임스페이스.
_NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}

# 기본 shell 이미지(프로세스 또는 그 부모가 이것이면 "쉘 작업"으로 본다).
DEFAULT_SHELL_IMAGES = ["cmd.exe", "powershell.exe", "pwsh.exe", "bash.exe", "sh.exe", "wsl.exe", "git.exe"]


def _boot_epoch() -> int:
    """부팅 시각(epoch sec). GetTickCount64(업타임 ms) 역산 — 컬렉터 재시작해도 같은 부팅이면 동일값."""
    try:
        up_ms = ctypes.windll.kernel32.GetTickCount64()  # type: ignore[attr-defined]
        return int(time.time() - up_ms / 1000.0)
    except Exception:
        return 0


def _basename_lower(path: str) -> str:
    if not path:
        return ""
    return path.replace("/", "\\").rsplit("\\", 1)[-1].lower()


class WinExecAudit:
    """Sysmon 프로세스생성 이벤트를 폴링해 정규화 이벤트 dict 로 만들어 spool 파일에 append."""

    def __init__(self, machine_id: str, spool_path: str, cursor_path: str,
                 log: str = "Microsoft-Windows-Sysmon/Operational",
                 shell_images: Optional[list[str]] = None,
                 deny_images: Optional[list[str]] = None,
                 max_per_poll: int = 500,
                 logger: Optional[Callable[[str], None]] = None) -> None:
        self.machine_id = machine_id or ""
        self.spool_path = Path(spool_path)
        self.cursor_path = Path(cursor_path)
        self.log = log
        self.shell_images = {s.lower() for s in (shell_images or DEFAULT_SHELL_IMAGES)}
        self.deny_images = {s.lower() for s in (deny_images or [])}
        self.max_per_poll = max_per_poll
        self._log = logger or (lambda m: None)
        self.session_id = f"osexec-{self.machine_id}-{_boot_epoch()}"
        self._cursor = self._load_cursor()
        self.available = os.name == "nt"

    # ---- 커서(마지막 처리한 RecordId) 영속 ----
    def _load_cursor(self) -> int:
        try:
            return int(json.loads(self.cursor_path.read_text(encoding="utf-8")).get("record_id", 0))
        except Exception:
            return 0

    def _save_cursor(self) -> None:
        try:
            self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cursor_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"record_id": self._cursor}), encoding="utf-8")
            os.replace(tmp, self.cursor_path)
        except OSError as e:
            self._log(f"[periscribe] audit 커서 저장 실패: {e}")

    # ---- 1회 폴 ----
    def poll(self) -> int:
        """새 exec 이벤트를 spool 에 append. 반환=append한 이벤트 수. 절대 예외를 던지지 않음."""
        if not self.available:
            return 0
        try:
            xml_text = self._query(self._cursor)
        except Exception as e:  # noqa: BLE001
            self._log(f"[periscribe] audit 쿼리 실패: {e}")
            return 0
        if not xml_text.strip():
            return 0
        events, max_rid = self.parse_events(xml_text)
        if not events:
            # 로그가 비워졌는데(wrap/clear) 커서가 과거에 머물러 영영 매칭 안 되는 것 방지:
            # 받은 게 없고 최신 RecordId가 커서보다 작아졌으면 커서 리셋.
            self._maybe_reset_cursor()
            return 0
        try:
            self.spool_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.spool_path, "a", encoding="utf-8") as f:
                for e in events:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            self._log(f"[periscribe] audit spool 쓰기 실패: {e}")
            return 0  # 커서 전진 안 함 → 다음 폴에 재시도(멱등이라 안전)
        if max_rid > self._cursor:
            self._cursor = max_rid
            self._save_cursor()
        return len(events)

    def _maybe_reset_cursor(self) -> None:
        try:
            xml_text = self._query(0, count=1, newest=True)
            _, max_rid = self.parse_events(xml_text) if xml_text else ([], 0)
            if max_rid and max_rid < self._cursor:
                self._cursor = 0
                self._save_cursor()
                self._log("[periscribe] audit 로그 리셋 감지 → 커서 초기화")
        except Exception:
            pass

    def _query(self, after_rid: int, count: Optional[int] = None, newest: bool = False) -> str:
        q = f"*[System[(EventID=1) and (EventRecordID>{after_rid})]]"
        cmd = ["wevtutil", "qe", self.log, f"/q:{q}", "/f:xml", "/e:Events",
               f"/c:{count or self.max_per_poll}", "/rd:" + ("true" if newest else "false")]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        if r.returncode != 0:
            return ""   # 로그 없음/권한 없음/Sysmon 미설치 → 조용히 빈 결과
        return r.stdout or ""

    # ---- XML → 정규화 이벤트(테스트 가능: 순수 함수) ----
    def parse_events(self, xml_text: str) -> tuple[list[dict[str, Any]], int]:
        out: list[dict[str, Any]] = []
        max_rid = 0
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return out, max_rid
        for ev in root.findall("e:Event", _NS):
            try:
                node, rid = self._map_event(ev)
            except Exception:
                node, rid = None, 0
            if rid > max_rid:
                max_rid = rid
            if node is not None:
                out.append(node)
        return out, max_rid

    def _map_event(self, ev: ET.Element) -> tuple[Optional[dict[str, Any]], int]:
        sysm = ev.find("e:System", _NS)
        data = {d.get("Name"): (d.text or "") for d in ev.findall("e:EventData/e:Data", _NS)}
        rid_txt = sysm.findtext("e:EventRecordID", default="0", namespaces=_NS) if sysm is not None else "0"
        try:
            rid = int(rid_txt)
        except (TypeError, ValueError):
            rid = 0
        image = data.get("Image", "")
        parent_image = data.get("ParentImage", "")
        img_b, par_b = _basename_lower(image), _basename_lower(parent_image)
        # 2차 노이즈 필터(Sysmon 설정이 1차). 프로세스 또는 부모가 shell 이어야, deny면 제외.
        if img_b in self.deny_images:
            return None, rid
        if not (img_b in self.shell_images or par_b in self.shell_images):
            return None, rid
        ut = data.get("UtcTime", "")
        ts = (ut.replace(" ", "T") + "Z") if ut else None
        node = {
            "_periscribe_event": 1,   # 파서 패스스루 신호(파서가 제거)
            "event_id": f"winexec-{self.machine_id}-{rid}",
            "schema_version": 1,
            "source": "os-exec",
            "machine_id": self.machine_id,
            "session_id": self.session_id,
            "kind": "process_exec",
            "tool": img_b or None,
            "ts": ts,
            "cwd": data.get("CurrentDirectory") or None,
            "payload": {
                "image": image,
                "command_line": data.get("CommandLine", ""),
                "parent_image": parent_image,
                "parent_command_line": data.get("ParentCommandLine", ""),
                "process_guid": data.get("ProcessGuid", ""),
                "parent_process_guid": data.get("ParentProcessGuid", ""),
                "pid": data.get("ProcessId", ""),
                "parent_pid": data.get("ParentProcessId", ""),
                "user": data.get("User", ""),
                "integrity": data.get("IntegrityLevel", ""),
            },
        }
        return node, rid
