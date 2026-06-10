"""apiproxy — 로컬 TLS 리버스 프록시. Claude↔Anthropic 트래픽을 도청·통제·로깅.

Claude 는 ANTHROPIC_BASE_URL=https://127.0.0.1:<port> 로 이 프록시에 붙는다(우리 CA 를 NODE_EXTRA_CA_CERTS
로 신뢰). 프록시는 요청을 정책으로 검사(차단/레닥션/주입)한 뒤 api.anthropic.com 으로 그대로 전달하고,
응답(SSE)을 **버퍼링 없이 실시간 중계**하며 복사본만 로깅한다. forward-first/fail-open: 로깅·파싱 예외가
중계를 절대 깨지 않는다. 127.0.0.1 전용 바인드. 표준 라이브러리만.
"""

from __future__ import annotations

import http.client
import json
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from . import apilog, proxyguard, proxypolicy

UPSTREAM_HOST = "api.anthropic.com"
# 그대로 전달하면 안 되는 hop-by-hop / 우리가 재계산하는 헤더(소문자).
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
        "trailer", "transfer-encoding", "upgrade", "content-length", "content-encoding",
        "host", "accept-encoding"}


class _Ctx:
    def __init__(self, machine_id: str, spool_path: str, policy_path: str,
                 logger: Optional[Callable[[str], None]]) -> None:
        self.machine_id = machine_id
        self.spool_path = Path(spool_path)
        self.policy_path = policy_path
        self._log = logger or (lambda m: None)
        self._lock = threading.Lock()

    def log(self, m: str) -> None:
        self._log(m)

    def write_events(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        try:
            self.spool_path.parent.mkdir(parents=True, exist_ok=True)
            line = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
            with self._lock:
                with open(self.spool_path, "a", encoding="utf-8") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
        except OSError as e:
            self.log(f"[periscribe] apilog spool 쓰기 실패: {e}")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # noqa: D401 - 조용히
        pass

    def _read_body(self) -> bytes:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        return self.rfile.read(n) if n > 0 else b""

    def _fwd_headers(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for k in self.headers.keys():
            if k.lower() in _HOP:
                continue
            out[k] = self.headers[k]
        out["Host"] = UPSTREAM_HOST
        return out

    def do_GET(self):
        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def _proxy(self, method: str) -> None:
        self.close_connection = True
        # 로컬 헬스 라우트: 업스트림 미전달로 즉시 200 "ok". guardian/proxy-setup 이 "프록시가 실제로
        # serve 중인가"(소켓+우리CA TLS+핸들러 생존)를 검증하는 데 쓴다.
        if self.path.split("?")[0].rstrip("/") == proxyguard.HEALTH_PATH:
            self._respond_health()
            return
        ctx: _Ctx = self.server.ctx  # type: ignore[attr-defined]
        body = self._read_body()
        is_messages = method == "POST" and self.path.split("?")[0].rstrip("/").endswith("/v1/messages")

        send_body = body
        session_id = None
        req_events: list[dict[str, Any]] = []
        if is_messages:
            try:
                parsed = json.loads(body.decode("utf-8", "replace"))
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                try:
                    session_id = apilog.session_id_for(parsed)
                except Exception:
                    session_id = None
                try:
                    policy = proxypolicy.load_policy(ctx.policy_path)
                    blocked, modified, reason = proxypolicy.apply_policy(policy, parsed)
                except Exception as e:  # noqa: BLE001
                    ctx.log(f"[periscribe] 정책 적용 오류(통과): {e}")
                    blocked, modified, reason = False, parsed, ""
                try:
                    req_events = apilog.events_from_request(parsed, ctx.machine_id, session_id or "api-?",
                                                            blocked, reason)
                except Exception:
                    req_events = []
                if blocked:
                    self._respond_block(reason)
                    ctx.write_events(req_events)
                    return
                if modified is not parsed:
                    try:
                        send_body = json.dumps(modified, ensure_ascii=False).encode("utf-8")
                    except Exception:
                        send_body = body

        # ---- 업스트림으로 전달 + 응답 실시간 중계 ----
        conn = None
        try:
            conn = http.client.HTTPSConnection(UPSTREAM_HOST, 443, timeout=600,
                                               context=ssl.create_default_context())
            headers = self._fwd_headers()
            if method == "POST":
                headers["Content-Length"] = str(len(send_body))
            conn.request(method, self.path,
                         body=(send_body if method == "POST" else None), headers=headers)
            resp = conn.getresponse()
        except Exception as e:  # noqa: BLE001
            ctx.log(f"[periscribe] 업스트림 연결 실패: {e}")
            try:
                self.send_error(502, "upstream error")
            except Exception:
                pass
            if conn:
                try: conn.close()
                except Exception: pass
            return

        acc: list[bytes] = []
        ctype = resp.getheader("Content-Type", "") or ""
        try:
            self.send_response_only(resp.status, resp.reason)
            for k, v in resp.getheaders():
                if k.lower() in _HOP:
                    continue
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = resp.read(16384)
                if not chunk:
                    break
                self.wfile.write(chunk)     # 중계 먼저
                self.wfile.flush()
                if is_messages:
                    acc.append(chunk)
        except Exception as e:  # noqa: BLE001
            ctx.log(f"[periscribe] 중계 중 오류: {e}")
            try: conn.close()
            except Exception: pass
            return
        finally:
            try: conn.close()
            except Exception: pass

        # ---- 로깅(best-effort, 중계엔 영향 없음) ----
        if is_messages and session_id:
            try:
                events = list(req_events)
                raw = b"".join(acc).decode("utf-8", "replace")
                if "event-stream" in ctype:
                    msg = apilog.assemble_sse(raw)
                else:
                    try:
                        msg = apilog.message_from_json(json.loads(raw))
                    except Exception:
                        msg = {"id": None, "blocks": []}
                events += apilog.events_from_message(msg, ctx.machine_id, session_id)
                ctx.write_events(events)
            except Exception as e:  # noqa: BLE001
                ctx.log(f"[periscribe] apilog 실패: {e}")

    def _respond_health(self) -> None:
        try:
            self.send_response_only(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"ok")
        except Exception:
            pass

    def _respond_block(self, reason: str) -> None:
        payload = json.dumps({"type": "error", "error": {
            "type": "permission_error",
            "message": f"Blocked by Periscribe policy ({reason})"}}).encode("utf-8")
        try:
            self.send_response_only(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except Exception:
            pass


class _ProxyServer(ThreadingHTTPServer):
    """Claude 는 모든 API 콜마다 새 TCP+TLS 연결을 만든다(우리가 Connection: close 를 보내므로).
    병렬 툴콜/서브에이전트면 동시 연결이 쉽게 6~8개를 넘는다 → 기본 backlog(5)면 ECONNREFUSED.
    또 리스닝 소켓을 통째로 wrap_socket 하면 TLS 핸드셰이크가 accept 루프(단일 스레드)에서 돌아
    반열림 커넥션 하나가 프록시 전체를 멈춘다. 그래서 backlog 를 키우고 핸드셰이크를 워커로 옮긴다."""

    request_queue_size = 128

    def __init__(self, addr, handler, sslctx: ssl.SSLContext) -> None:
        self.sslctx = sslctx
        super().__init__(addr, handler)

    def finish_request(self, request, client_address):
        # TLS 핸드셰이크를 워커 스레드에서 수행(타임아웃 한정). 실패는 조용히 폐기.
        try:
            request.settimeout(10.0)
            tls = self.sslctx.wrap_socket(request, server_side=True)
            tls.settimeout(None)
        except Exception:
            try:
                request.close()
            except OSError:
                pass
            return
        try:
            super().finish_request(tls, client_address)
        finally:
            try:
                tls.close()  # FIN 즉시 보장(close-delimited 응답이 refcount 에 매달리지 않게)
            except OSError:
                pass


def _make_server(machine_id: str, port: int, spool_path: str, policy_path: str,
                 cert_pem: str, cert_key: str,
                 logger: Optional[Callable[[str], None]] = None) -> _ProxyServer:
    """실서비스/테스트 공용 서버 조립. 테스트도 이걸 써야 실제 backlog/핸드셰이크 경로를 검증한다."""
    proxypolicy.ensure_policy_file(policy_path)
    sslctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sslctx.load_cert_chain(certfile=cert_pem, keyfile=cert_key)
    httpd = _ProxyServer(("127.0.0.1", port), _Handler, sslctx)
    httpd.ctx = _Ctx(machine_id, spool_path, policy_path, logger)  # type: ignore[attr-defined]
    return httpd


def run_proxy(machine_id: str, port: int, spool_path: str, policy_path: str,
              cert_pem: str, cert_key: str,
              logger: Optional[Callable[[str], None]] = None) -> None:
    """프록시를 127.0.0.1:<port> 에서 serve_forever. (proxy-run 서브커맨드가 호출)"""
    httpd = _make_server(machine_id, port, spool_path, policy_path, cert_pem, cert_key, logger)
    if logger:
        logger(f"[periscribe] API 프록시 리슨 https://127.0.0.1:{port} (spool={spool_path})")
    httpd.serve_forever()
