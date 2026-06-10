"""Collector — 폴링 루프로 감시 디렉터리의 *.jsonl 을 tail -> 파싱 -> 적재.

흐름(spec §3.3, §6):
1. watch_dir 하위 *.jsonl 발견(새 파일 포함).
2. 각 파일에서 새 완성 줄을 읽어 파싱(파일별 오프셋).
3. 폴링 1회분 이벤트를 배치 insert(멱등).
4. insert 성공 후에만 오프셋 체크포인트 영속. 실패 시 전진 안 하고 다음 폴링에 재시도.
"""

from __future__ import annotations

import os
import re
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import __version__, crypto
from .checkpoint import Checkpoint
from .config import Config
from .parser import Parser
from .sink import Sink, SinkAuthError, SinkError
from .tailer import Tailer, file_inode, initial_offset


def _child_env() -> dict:
    """frozen exe 를 subprocess 로 띄울 때 PyInstaller 가 심은 _MEI 포인터 변수를 제거한 환경.
    안 지우면 자식 onefile 부트로더가 부모의 _MEI 추출폴더를 재사용하려다 'no such file' 로 죽는다
    (다른 onefile exe 를 spawn 할 때 = proxy.exe → periscribe.exe 경우 치명)."""
    env = os.environ.copy()
    for v in ("_MEIPASS2", "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL",
              "_PYI_APPLICATION_HOME_DIR"):
        env.pop(v, None)
    return env


class Collector:
    def __init__(self, config: Config, sink: Sink) -> None:
        self.config = config
        self.sink = sink
        self.parser = Parser(
            machine_id=config.machine_id,
            source=config.source,
            store_thinking=config.store_thinking,
            store_raw=config.store_raw,
            redact=config.redact,
        )
        self.checkpoint = Checkpoint(config.checkpoint_path)
        # 체크포인트가 비어 있으면 "이 PC 최초 실행"(과거 폭주 방지 EOF/backfill 적용 대상).
        # 비어 있지 않으면 "재시작" → 체크포인트 없는 새 파일은 처음부터 읽어 다운타임 세션을 놓치지 않음.
        self.fresh_start = self.checkpoint.is_empty()
        self.tailers: dict[str, Tailer] = {}
        self._running = False

        # 헬스(하트비트): sink가 beat()를 지원하고 interval>0 일 때만(dry-run 등에서는 비활성).
        self._heartbeat_enabled = config.heartbeat_interval > 0 and hasattr(sink, "beat")
        self._last_beat = 0.0

        # E2EE: 암호화 적재 필수 여부(설정 on + sink가 DEK를 지원할 때). StdoutSink(dry-run)는 제외.
        self._enc_required = config.encrypt and hasattr(sink, "has_dek")
        self._enc_hold_logged = False
        # 세션 카탈로그: 마지막으로 보낸 목록 서명(변경 시에만 재전송).
        self._last_catalog_sig: int | None = None

        # 관측: 최근 오류를 하트비트에 실어 보낸다(웹에서 머신별 표시).
        self._last_error = ""
        self._last_drop_seen = ""        # poison 스킵 메시지 변화 감지용
        # 401(revoked/삭제) 백오프·자가종료.
        self._auth_fail = 0
        self._auth_fail_max = 10         # 이만큼 연속 401이면 죽은 토큰으로 보고 종료
        self._auth_backoff_cap = 300.0   # 지수 백오프 상한(초)

        # 파일 로그(선택). 비우면 stderr만 사용.
        self._log_path = config.log_file or None
        if self._log_path:
            Path(self._log_path).parent.mkdir(parents=True, exist_ok=True)

        # 재시작 시 이미 DEK가 있으면 sink에 주입(공개키는 첫 하트비트에 갱신·재봉인).
        if self._enc_required and config.dek:
            try:
                sink.set_dek(crypto.dek_from_b64(config.dek), config.dek_kid)  # type: ignore[attr-defined]
            except Exception as e:
                self._last_error = f"DEK 로드 실패: {e}"
                self._log(f"[periscribe] DEK 로드 실패(재부트스트랩 시도): {e}")

        # OS 레벨 exec 감사(옵션, Windows). 활성 시 매 루프 정규화 이벤트를 watch_dir/_osexec spool 에
        # 기록 → 아래 discover()가 그 *.jsonl 을 픽업해 기존 파이프라인으로 수집(코드 재사용).
        self._audit = None
        self._last_audit_poll = 0.0
        if config.os_exec_enabled and os.name == "nt":
            try:
                from .audit_win import WinExecAudit
                safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", config.machine_id or "host") or "host"
                spool = Path(config.watch_dir) / "_osexec" / f"{safe}.jsonl"
                cursor = Path(config.checkpoint_path).with_name("sysmon_cursor.json")
                self._audit = WinExecAudit(
                    machine_id=config.machine_id, spool_path=str(spool), cursor_path=str(cursor),
                    log=config.os_exec_log, root_patterns=config.os_exec_root_patterns,
                    deny_images=config.os_exec_deny_images, logger=self._log,
                )
                self._log(f"[periscribe] OS exec 감사 활성(session={self._audit.session_id})")
            except Exception as e:  # noqa: BLE001
                self._log(f"[periscribe] OS exec 감사 초기화 실패: {e}")

        # Claude API 게이트웨이 프록시(옵션, Windows). api_log_enabled면 별도 supervised subprocess.
        self._proxy_proc: Optional[subprocess.Popen] = None
        self._proxy_last_spawn = 0.0
        self._proxy_enabled = bool(getattr(config, "api_log_enabled", False)) and os.name == "nt"
        self._proxy_status_check = 0.0  # 프록시 pause 상태 표면화 throttle
        self._last_alive = 0.0          # guardian 이 읽는 생존 신호(alive 파일) touch throttle
        self._last_autostart_sync = 0.0  # 자가치유 자동시작 reconcile throttle(exe 이동 추종)

    # ---- E2EE: 하트비트 응답의 공개키 처리 + DEK 부트스트랩 ----
    def _handle_enc(self, resp: dict[str, Any] | None) -> None:
        if not resp or not self._enc_required or not hasattr(self.sink, "set_public_key"):
            return
        enc = resp.get("enc") or {}
        pub = enc.get("public_key")
        if not pub:
            return
        self.sink.set_public_key(pub)                 # type: ignore[attr-defined]
        # 아직 이 머신용 DEK가 없으면 로컬 생성·영속(패스프레이즈 불필요). 다음 하트비트에 봉인본 동봉.
        # kid = DEK '세대' 식별자(랜덤). 재설치하면 새 세대가 생겨 dek_keys에 누적된다(옛 로그 복호 유지).
        if not self.sink.has_dek():                    # type: ignore[attr-defined]
            dek = crypto.gen_dek()
            kid = secrets.randbits(31) or 1
            self.config.persist_dek(crypto.dek_to_b64(dek), kid)
            self.sink.set_dek(dek, kid)                # type: ignore[attr-defined]
            self._log(f"[periscribe] 암호화 키(per-device DEK) 생성·등록 (kid={kid})")

    # ---- 파일 발견 ----
    def discover(self) -> list[str]:
        files: list[str] = []
        root = Path(self.config.watch_dir)
        if root.exists():
            files.extend(str(p) for p in root.rglob("*.jsonl"))
        # 컨테이너 transcript 루트도 감시(설정 시).
        if self.config.container_root:
            croot = Path(self.config.container_root)
            if croot.exists():
                files.extend(str(p) for p in croot.rglob("*.jsonl"))
        return files

    def _project_folder(self, file_path: str) -> str:
        # ~/.claude/projects/<folder>/<session>.jsonl -> <folder>
        return Path(file_path).parent.name

    def _container_id_for(self, file_path: str) -> str | None:
        """파일이 container_root 아래면 첫 경로 세그먼트를 container_id로 반환."""
        if not self.config.container_root:
            return None
        try:
            rel = Path(file_path).resolve().relative_to(Path(self.config.container_root).resolve())
        except (ValueError, OSError):
            return None
        return rel.parts[0] if rel.parts else None

    def _ensure_tailer(self, file_path: str, first_run: bool) -> Tailer:
        existing = self.tailers.get(file_path)
        if existing is not None:
            return existing

        saved = self.checkpoint.get(file_path)
        if saved is not None:
            # 이미 추적하던 파일: 마지막 확정 오프셋부터 재개(다운타임 중 늘어난 분 포함).
            tailer = Tailer(file_path, offset=saved.get("offset", 0), inode=saved.get("inode"))
        elif first_run and self.fresh_start:
            # 이 PC 최초 실행 시점에 이미 있던 파일: 기본 EOF부터(또는 backfill). 과거 폭주 방지.
            off = initial_offset(file_path, self.config.backfill)
            try:
                ino = file_inode(file_path)
            except OSError:
                ino = 0
            tailer = Tailer(file_path, offset=off, inode=ino)
        else:
            # 처음부터: (a) 실행 중 새로 생긴 파일, (b) 재시작 시 체크포인트 없는 새 파일
            #          (= collector 꺼져 있는 동안 시작된 세션). 둘 다 통째로 수집.
            tailer = Tailer(file_path, offset=0)

        self.tailers[file_path] = tailer
        return tailer

    # ---- 한 파일 1회 처리 ----
    def _process_file(self, file_path: str, first_run: bool) -> tuple[int, dict[str, Any]]:
        tailer = self._ensure_tailer(file_path, first_run)
        lines, new_offset = tailer.read_new_lines()
        if not lines:
            return 0, {}

        project = self._project_folder(file_path)
        container_id = self._container_id_for(file_path)
        events: list[dict[str, Any]] = []
        for line in lines:
            events.extend(self.parser.parse_line(line, project, container_id))

        # 적재(배치). 실패하면 오프셋 전진 안 함 -> 다음 폴링 재시도(멱등이라 안전).
        last_resp: dict[str, Any] = {}
        if events:
            for batch in _chunks(events, self.config.batch_size):
                r = self.sink.emit(batch)  # 실패 시 SinkError 전파
                if r:
                    last_resp = r

        # 적재 확정 후에만 오프셋 영속
        tailer.commit(new_offset)
        self.checkpoint.set(file_path, new_offset, tailer.inode)
        return len(events), last_resp

    # ---- 백필: 서버가 보낸 session_id의 로컬 파일을 처음부터 재적재(멱등) ----
    def _apply_backfill(self, session_ids: set[str]) -> None:
        for sid in session_ids:
            if not sid:
                continue
            n = self._reset_session(sid)
            self._log(f"[periscribe] 백필 요청 수신: session={sid} → 파일 {n}개 처음부터 재적재")

    def _reset_session(self, session_id: str) -> int:
        # transcript 파일명 stem == session_id, 또는 사이드체인이 session_id 폴더 아래에 있음.
        targeted = [f for f in self.discover()
                    if Path(f).stem == session_id or session_id in Path(f).parts]
        for f in targeted:
            self.checkpoint.reset(f)     # 저장 오프셋 제거
            self.tailers.pop(f, None)    # 다음 폴링에 offset 0부터 새 tailer 생성
        return len(targeted)

    # ---- 삭제: 관리자가 웹에서 "세션 삭제" → 로컬 transcript 파일까지 제거(부활 방지) ----
    def _apply_deletes(self, session_ids: set[str]) -> None:
        for sid in session_ids:
            if not sid:
                continue
            n = self._delete_session(sid)
            self._log(f"[periscribe] 세션 삭제 명령 수신: session={sid} → 로컬 파일 {n}개 삭제")

    # ---- API 게이트웨이 프록시 supervise(별도 프로세스로 항상 떠 있게) ----
    @staticmethod
    def _port_alive(port: int) -> bool:
        import socket
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
                return True
        except OSError:
            return False

    def _supervise_proxy(self) -> None:
        if self._proxy_proc is not None and self._proxy_proc.poll() is None:
            return  # 우리가 띄운 프록시 살아있음
        now = time.time()
        if now - self._proxy_last_spawn < 5.0:
            return  # 재기동 백오프
        # 우리가 추적 안 하지만(예: 컬렉터 재시작) 포트가 이미 살아있으면 중복 기동 안 함.
        if self._proxy_proc is None and self._port_alive(self.config.api_proxy_port):
            return
        self._proxy_last_spawn = now
        cfgpath = self.config.source_path or "config.json"
        if getattr(sys, "frozen", False):
            args = [sys.executable, "proxy-run", "-c", cfgpath]
        else:
            pyw = str(Path(sys.executable).with_name("pythonw.exe"))
            args = [pyw, "-m", "periscribe", "proxy-run", "-c", cfgpath]
        flags = 0x00000008 | 0x08000000 if os.name == "nt" else 0  # DETACHED | NO_WINDOW
        try:
            self._proxy_proc = subprocess.Popen(args, env=_child_env(), creationflags=flags, close_fds=True)
            self._log("[periscribe] API 프록시 기동")
        except Exception as e:  # noqa: BLE001
            self._log(f"[periscribe] API 프록시 기동 실패: {e}")

    def _touch_alive(self) -> None:
        """guardian 의 컬렉터 watchdog 이 읽을 생존 신호. 로그는 이벤트 있을 때만 써서 mtime 으로는
        유휴 컬렉터를 죽은 걸로 오판한다 → 매 루프 alive 파일을 갱신(10s throttle)."""
        now = time.time()
        if now - self._last_alive < 10.0:
            return
        self._last_alive = now
        try:
            from . import proxyguard
            p = proxyguard.data_dir() / "collector.alive"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(int(now)), encoding="utf-8")
        except Exception:
            pass

    def _reconcile_autostart(self) -> None:
        """자가치유 자동시작: 실행 중 collector exe 가 옮겨졌을 때 HKCU Run 값을 현재 위치로 맞춘다(5분 throttle).
        고정 설치 위치를 두지 않으므로 '실행 중인 위치'가 단일 진실의 원천 → exe 를 어디 두든 자동시작 유지."""
        now = time.time()
        if now - self._last_autostart_sync < 300.0:
            return
        self._last_autostart_sync = now
        try:
            from . import __main__ as m
            m._reconcile_autostart()
        except Exception:
            pass  # 보조 기능 — 실패해도 본 수집에 영향 없음

    # 프록시 failsafe 가 env 를 빼서(직결 fail-open) API 로깅이 일시중지된 상태를 헬스바에 표면화.
    # guardian 이 env 를 빼면 settings.json 에 ANTHROPIC_BASE_URL 이 사라진다 → 그걸 감지해 last_error 로 알림.
    _PROXY_PAUSE_MSG = "API 로깅 일시중지 — 프록시 비정상(자동 직결)"

    def _update_proxy_pause_status(self) -> None:
        now = time.time()
        if now - self._proxy_status_check < 5.0:
            return
        self._proxy_status_check = now
        try:
            from . import proxyguard
            paused = not proxyguard.env_has_proxy()
        except Exception:
            return
        if paused:
            self._last_error = self._PROXY_PAUSE_MSG
        elif self._last_error == self._PROXY_PAUSE_MSG:
            self._last_error = ""  # 복구됨(우리가 세팅한 안내만 해제; 실제 오류는 보존)

    def _delete_session(self, session_id: str) -> int:
        # _reset_session 과 동일 매칭(메인 + 사이드체인 agent-* 파일). 파일 제거 + 체크포인트/타일러 정리.
        targeted = [f for f in self.discover()
                    if Path(f).stem == session_id or session_id in Path(f).parts]
        removed = 0
        for f in targeted:
            try:
                Path(f).unlink()
            except FileNotFoundError:
                pass  # 이미 없음 = 성공으로 간주
            except OSError as e:
                self._log(f"[periscribe] 파일 삭제 실패 {f}: {e}")
                continue  # 체크포인트 보존(다음 기회에 재시도 여지)
            self.checkpoint.reset(f)     # 저장 오프셋 제거
            self.tailers.pop(f, None)    # 추적 중단
            removed += 1
        # 파일이 사라져 다음 하트비트에서 카탈로그가 자동 갱신됨(세션 재출현 없음).
        return removed

    # ---- 로깅 (stderr + 선택적 파일, 크기 기반 로테이션) ----
    def _log(self, msg: str) -> None:
        line = msg if msg.endswith("\n") else msg + "\n"
        print(msg, file=sys.stderr, flush=True)
        if not self._log_path:
            return
        try:
            self._rotate_if_needed()
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass  # 로깅 실패가 수집을 막지 않게

    def _rotate_if_needed(self) -> None:
        p = self._log_path
        try:
            if os.path.getsize(p) < self.config.log_max_bytes:
                return
        except OSError:
            return
        # p -> p.1 -> p.2 ... (오래된 것 폐기)
        for i in range(self.config.log_backups, 0, -1):
            src = p if i == 1 else f"{p}.{i - 1}"
            dst = f"{p}.{i}"
            if os.path.exists(src):
                try:
                    if os.path.exists(dst):
                        os.remove(dst)
                    os.replace(src, dst)
                except OSError:
                    pass

    # ---- 세션 카탈로그(로컬에 존재하는 세션 목록) ----
    def _build_catalog(self) -> list[dict[str, Any]]:
        """watch 대상의 메인 세션 파일 목록(내용 미적재 포함). 사이드체인(agent-*)은 제외."""
        out: list[dict[str, Any]] = []
        for f in self.discover():
            p = Path(f)
            if p.stem.startswith("agent-"):
                continue
            if p.parent.name in ("_osexec", "_apilog"):
                continue  # OS exec / API 게이트웨이 spool 은 카탈로그에 안 띄움(이벤트는 정상 적재)
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({
                "session_id": p.stem,
                "project": self._project_folder(f),
                "container_id": self._container_id_for(f),
                "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
                "size": st.st_size,
            })
        return out

    def _catalog_and_sig(self) -> tuple[list[dict[str, Any]], int]:
        cat = self._build_catalog()
        sig = hash(tuple((c["session_id"], c["mtime"], c["size"]) for c in cat))
        return cat, sig

    # ---- 하트비트 ----
    def _maybe_heartbeat(self) -> dict[str, Any] | None:
        if not self._heartbeat_enabled:
            return None
        now = time.time()
        if now - self._last_beat < self.config.heartbeat_interval:
            return None
        cat, sig = self._catalog_and_sig()
        send_cat = cat if sig != self._last_catalog_sig else None  # 변경됐을 때만 전송
        try:
            resp = self.sink.beat(send_cat)  # 빈 ingest → last_seen 갱신 + 백필 요청 + (변경 시) 카탈로그
            self._last_beat = now
            if send_cat is not None:
                self._last_catalog_sig = sig  # 성공 후에만 갱신(실패 시 다음에 재전송)
            return resp
        except SinkAuthError:
            raise  # 상위 루프에서 백오프/종료 처리
        except Exception as e:
            self._set_error(f"하트비트 실패: {e}")
            self._log(f"[periscribe] 하트비트 실패: {e}")
            return None

    def _set_error(self, msg: str) -> None:
        self._last_error = msg

    def _interruptible_sleep(self, secs: float) -> None:
        """길어도 종료(_running=False)에 빨리 반응하도록 잘게 나눠 잔다."""
        end = time.time() + max(0.0, secs)
        while self._running and time.time() < end:
            time.sleep(min(0.4, end - time.time()))

    # ---- 메인 루프 ----
    def run(self) -> None:
        self._running = True
        self._install_signal_handlers()
        first_run = True
        self._log(f"[periscribe] v{__version__} watch={self.config.watch_dir} machine_id={self.config.machine_id}")
        self._log(f"[periscribe] poll={self.config.poll_interval}s backfill={self.config.backfill} "
                  f"redact={self.config.redact} heartbeat={self.config.heartbeat_interval}s")

        while self._running:
            self._touch_alive()  # guardian 이 "컬렉터 살아있음"을 신뢰성 있게 판정하도록 매 루프 신호
            self._reconcile_autostart()  # 자가치유: exe 이동 시 자동시작 경로 추종(5분 throttle)
            # 직전 오류를 하트비트에 실어 보낸다(웹에서 머신별 표시).
            if hasattr(self.sink, "set_last_error"):
                self.sink.set_last_error(self._last_error)
            try:
                backfill_ids: set[str] = set()
                delete_ids: set[str] = set()
                iter_error = None
                resp = self._maybe_heartbeat()
                if resp:
                    backfill_ids.update(resp.get("backfill") or [])
                    delete_ids.update(resp.get("delete_local") or [])
                    self._handle_enc(resp)

                # 암호화 필수인데 키가 아직 준비 안 됨(관리자 미설정/네트워크) → 평문 적재 금지, 보류.
                # transcript는 디스크에 그대로 남고 오프셋도 전진 안 하므로 키 준비 후 손실 없이 재개.
                if self._enc_required and not self.sink.has_dek():  # type: ignore[attr-defined]
                    if not self._enc_hold_logged:
                        self._log("[periscribe] 암호화 키 대기 중(웹에서 암호화 설정 필요) → 적재 보류")
                        self._enc_hold_logged = True
                    self._last_error = "암호화 키 대기 중 — 적재 보류"
                    self._interruptible_sleep(self.config.poll_interval)
                    continue
                self._enc_hold_logged = False

                # OS exec 감사 폴(throttle). spool 에 기록 → 아래 discover/_process_file 가 수집.
                if self._audit is not None:
                    now_t = time.time()
                    if now_t - self._last_audit_poll >= self.config.os_exec_poll_interval:
                        self._last_audit_poll = now_t
                        try:
                            self._audit.poll()
                        except Exception as e:  # noqa: BLE001
                            self._log(f"[periscribe] OS exec 감사 폴 오류: {e}")

                # API 프록시 supervise(죽었으면 재기동). spool(_apilog)은 아래 discover가 수집.
                if self._proxy_enabled:
                    self._supervise_proxy()
                    self._update_proxy_pause_status()

                files = self.discover()
                total = 0
                for fp in files:
                    try:
                        cnt, fresp = self._process_file(fp, first_run)
                        total += cnt
                        if fresp:
                            backfill_ids.update(fresp.get("backfill") or [])
                            delete_ids.update(fresp.get("delete_local") or [])
                    except SinkAuthError:
                        raise  # 아래 핸들러로 → 백오프/종료
                    except SinkError as e:
                        # 네트워크/일시 실패: 이 파일 오프셋은 전진 안 됨. 다음 폴링 재시도.
                        iter_error = f"적재 실패(재시도): {e}"
                        self._log(f"[periscribe] sink 실패(재시도 예정): {e}")
                    except Exception as e:
                        iter_error = f"파일 처리 오류: {e}"
                        self._log(f"[periscribe] 파일 처리 오류 {fp}: {e}")
                if total:
                    self._log(f"[periscribe] +{total} events")
                if backfill_ids:
                    self._apply_backfill(backfill_ids)
                if delete_ids:
                    self._apply_deletes(delete_ids)
                first_run = False
                self._auth_fail = 0  # 정상 한 바퀴 → 401 카운터 리셋

                # 이번 바퀴 오류 상태 갱신: poison 스킵 변화도 반영, 깨끗하면 클리어.
                drop = getattr(self.sink, "last_drop", "")
                if drop and drop != self._last_drop_seen:
                    self._last_drop_seen = drop
                    iter_error = iter_error or drop
                self._last_error = iter_error or ""
            except SinkAuthError as e:
                self._auth_fail += 1
                self._set_error(f"인증 거부(revoked/삭제?): {e}")
                self._log(f"[periscribe] 401 인증 거부 #{self._auth_fail}/{self._auth_fail_max}: {e}")
                if self._auth_fail >= self._auth_fail_max:
                    self._log("[periscribe] 토큰이 무효(revoked/삭제)로 판단 → 수집기 종료.")
                    self._running = False
                    break
                backoff = min(self._auth_backoff_cap, 5.0 * (2 ** (self._auth_fail - 1)))
                self._interruptible_sleep(backoff)
                continue
            except Exception as e:
                self._log(f"[periscribe] 루프 오류: {e}")
            self._interruptible_sleep(self.config.poll_interval)

        self._log("[periscribe] 종료")

    def stop(self, *_: Any) -> None:
        self._running = False

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.stop)
            except (ValueError, OSError):
                pass  # 메인 스레드가 아니면 무시


def _chunks(seq: list[Any], n: int):
    n = max(1, n)
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
