"""apiproxy — 로컬 TLS 리버스 프록시. Claude↔Anthropic 트래픽을 도청·통제·로깅.

Claude 는 ANTHROPIC_BASE_URL=https://127.0.0.1:<port> 로 이 프록시에 붙는다(우리 CA 를 NODE_EXTRA_CA_CERTS
로 신뢰). 프록시는 요청을 정책으로 검사(차단/레닥션/주입)한 뒤 api.anthropic.com 으로 그대로 전달하고,
응답(SSE)을 **버퍼링 없이 실시간 중계**하며 복사본만 로깅한다. forward-first/fail-open: 로깅·파싱 예외가
중계를 절대 깨지 않는다. 127.0.0.1 전용 바인드. 표준 라이브러리만.
"""

from __future__ import annotations

import hashlib
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


def build_message_response(model: Optional[str], text_blocks: list[str], notice: str,
                           stream: bool) -> tuple[str, bytes]:
    """Anthropic Messages 응답을 '진짜처럼' 합성한다(Claude 가 정상 서버 응답으로 인식).
    text_blocks(보존할 선행 텍스트) + notice(차단 안내)를 assistant 텍스트로, stop_reason=end_turn.
    tool_use 는 넣지 않는다 → Claude 가 실행할 도구가 없어 차단이 강제된다. stream 이면 SSE, 아니면 JSON.
    요청 차단(Section 1)·응답 게이팅(Section 2) 공용."""
    parts = [t for t in (text_blocks or []) if isinstance(t, str) and t]
    if notice:
        parts.append(notice)
    if not parts:
        parts = [""]   # 최소 1블록 보장(빈 content 응답 회피)
    mid = "msg_periscribe_" + hashlib.sha1(
        (str(model) + "\x1f" + "\x1f".join(parts)).encode("utf-8")).hexdigest()[:20]
    mdl = model or "claude"
    usage = {"input_tokens": 1, "output_tokens": 1}
    if stream:
        evs: list[tuple[str, dict]] = [("message_start", {
            "type": "message_start", "message": {
                "id": mid, "type": "message", "role": "assistant", "model": mdl,
                "content": [], "stop_reason": None, "stop_sequence": None, "usage": usage}})]
        for i, txt in enumerate(parts):
            evs.append(("content_block_start", {"type": "content_block_start", "index": i,
                                                "content_block": {"type": "text", "text": ""}}))
            evs.append(("content_block_delta", {"type": "content_block_delta", "index": i,
                                                "delta": {"type": "text_delta", "text": txt}}))
            evs.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
        evs.append(("message_delta", {"type": "message_delta",
                                      "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                      "usage": {"output_tokens": 1}}))
        evs.append(("message_stop", {"type": "message_stop"}))
        sse = "".join(f"event: {ev}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n" for ev, d in evs)
        return "text/event-stream; charset=utf-8", sse.encode("utf-8")
    body = {"id": mid, "type": "message", "role": "assistant", "model": mdl,
            "content": [{"type": "text", "text": t} for t in parts],
            "stop_reason": "end_turn", "stop_sequence": None, "usage": usage}
    return "application/json", json.dumps(body, ensure_ascii=False).encode("utf-8")


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
        # 로컬 헬스 라우트: 업스트림 미전달로 즉시 200 "ok". proxy on(_proxy_enable) 이 "프록시가 실제로
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
        # 응답 게이팅(Section 2) 컨텍스트 — 요청 파싱 단계에서 정책을 캡처해 응답 처리에 넘긴다.
        gate_tools = False
        tool_patterns: Any = []
        tool_msg = "파일 삭제 - 차단"
        req_stream = False
        req_model: Optional[str] = None
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
                policy: dict[str, Any] = {}
                try:
                    policy = proxypolicy.load_policy(ctx.policy_path)
                    blocked, modified, reason = proxypolicy.apply_policy(policy, parsed)
                    gate_tools = bool(policy.get("gate_tool_use"))
                    tool_patterns = policy.get("tool_block_patterns") or []
                    tool_msg = policy.get("tool_block_message") or "파일 삭제 - 차단"
                    req_stream = bool(parsed.get("stream"))
                    req_model = parsed.get("model")
                except Exception as e:  # noqa: BLE001
                    ctx.log(f"[periscribe] 정책 적용 오류(통과): {e}")
                    blocked, modified, reason = False, parsed, ""
                try:
                    req_events = apilog.events_from_request(parsed, ctx.machine_id, session_id or "api-?",
                                                            blocked, reason)
                except Exception:
                    req_events = []
                if blocked:
                    # 차단: 업스트림 미전송. 프록시가 합성 assistant 응답(block_message)으로 직접 대답.
                    self._respond_block_reply(parsed, policy.get("block_message") or "취소")
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

        # ---- 응답측 게이팅(Section 2): 켜져 있고 messages 200 이면 버퍼→검사→전송 ----
        if gate_tools and is_messages and resp.status == 200:
            try:
                self._gate_response(resp, ctx, session_id, req_model, req_stream,
                                    tool_patterns, tool_msg, req_events)
            finally:
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

    def _send_full(self, status: int, ctype: str, body: bytes,
                   extra_headers: Optional[list[tuple[str, str]]] = None) -> None:
        """완성된 본문을 한 번에 응답(close-delimited 대신 Content-Length). 합성/패스스루 공용."""
        try:
            self.send_response_only(status)
            for k, v in (extra_headers or []):
                self.send_header(k, v)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except Exception:
            pass

    def _respond_block_reply(self, parsed: Any, message: str) -> None:
        """요청 차단(Section 1): Anthropic 대신 프록시가 합성 assistant 응답으로 '대답'한다.
        Claude 는 정상 서버 응답으로 인식 → 에러 없이 message 표시, 세션 무손상."""
        model = parsed.get("model") if isinstance(parsed, dict) else None
        stream = bool(parsed.get("stream")) if isinstance(parsed, dict) else False
        ctype, body = build_message_response(model, [], message, stream)
        self._send_full(200, ctype, body)

    def _gate_response(self, resp, ctx: "_Ctx", session_id: Optional[str], model: Optional[str],
                       stream: bool, patterns: Any, notice: str,
                       req_events: list[dict[str, Any]]) -> None:
        """응답측 게이팅(Section 2): 응답을 전부 버퍼해 위험 tool_use 를 검사.
        미매칭 → 원본 바이트 무손실 전송. 매칭 → 선행 텍스트 보존 + tool_use 전부 드롭 + notice 합성.
        파싱/버퍼 예외는 fail-open(원본 전송)으로 절대 차단이 스트림을 깨지 않게."""
        ctype = resp.getheader("Content-Type", "") or ""
        raw = b""
        try:
            while True:
                chunk = resp.read(16384)
                if not chunk:
                    break
                raw += chunk
        except Exception as e:  # noqa: BLE001
            ctx.log(f"[periscribe] 게이팅 버퍼 실패(원본 전달): {e}")
        msg: Optional[dict[str, Any]] = None
        try:
            text = raw.decode("utf-8", "replace")
            if "event-stream" in ctype:
                msg = apilog.assemble_sse(text)
            else:
                msg = apilog.message_from_json(json.loads(text))
        except Exception:
            msg = None
        blocked_tools: list[str] = []
        if msg:
            try:
                blocked_tools = proxypolicy.match_blocked_tools(msg.get("blocks") or [], patterns)
            except Exception:
                blocked_tools = []

        if msg and blocked_tools:
            text_blocks = [b.get("text", "") for b in msg["blocks"]
                           if b.get("type") == "text" and b.get("text")]
            out_ctype, out_bytes = build_message_response(model, text_blocks, notice, stream)
            self._send_full(200, out_ctype, out_bytes)
        else:
            # 패스스루: 원본 헤더(hop 제외) 유지 + 우리 Content-Length 로 close-delimited.
            extra = [(k, v) for k, v in resp.getheaders()
                     if k.lower() not in _HOP and k.lower() != "content-type"]
            self._send_full(resp.status, ctype or "application/json", raw, extra)

        # ---- 로깅: 요청 이벤트 + 응답 이벤트(차단된 tool_use 는 blocked 표기) ----
        if session_id and msg is not None:
            try:
                events = list(req_events)
                resp_events = apilog.events_from_message(msg, ctx.machine_id, session_id)
                if blocked_tools:
                    bset = set(blocked_tools)
                    for ev in resp_events:
                        if ev.get("kind") == "tool_use" and ev.get("tool") in bset:
                            ev.setdefault("payload", {})["blocked"] = True
                            ev["payload"]["block_reason"] = "tool_block_pattern"
                            ev["is_error"] = True
                events += resp_events
                ctx.write_events(events)
            except Exception as e:  # noqa: BLE001
                ctx.log(f"[periscribe] apilog(게이팅) 실패: {e}")
        elif req_events:
            ctx.write_events(req_events)


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
