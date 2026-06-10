"""프록시 동시성 회귀 테스트 — "테스트는 통과하는데 실사용(Claude)은 끊어짐" 재발 방지.

Claude Code 는 병렬 툴콜/서브에이전트로 동시 6~8+ 연결을 일상적으로 만들고, 프록시가
Connection: close 를 보내므로 모든 API 콜이 새 TCP+TLS 연결이다. 과거 조립(backlog 5 +
리스닝 소켓 wrap → accept 루프 내 핸드셰이크)에서는 동시 8개부터 ECONNREFUSED,
반열림 TCP 1개로 전체 정지였다(2026-06-10 Node/undici 실측). 이 테스트는 실제 조립
(apiproxy._make_server)을 stdlib 클라이언트로 그 시나리오 그대로 친다.

업스트림은 http.client.HTTPSConnection monkeypatch 로 로컬 가짜 서버(평문 HTTP)로 우회
— apiproxy 코드는 무수정 경로."""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PROXY_PORT = 8094
UP_PORT = 8095


def _isolate(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    appdata = tmp_path / "appdata"
    home.mkdir(parents=True, exist_ok=True)
    appdata.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(appdata))


class _Upstream(BaseHTTPRequestHandler):
    """가짜 Anthropic. stream:true 면 chunked SSE 드립, 아니면 JSON."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            events = [("message_start", {"type": "message_start",
                                         "message": {"id": "msg_t", "role": "assistant", "content": []}})]
            events += [("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                "delta": {"type": "text_delta", "text": f"t{i} "}}) for i in range(10)]
            events.append(("message_stop", {"type": "message_stop"}))
            for ev, data in events:
                chunk = f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode()
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.flush()
                time.sleep(0.05)
            self.wfile.write(b"0\r\n\r\n")
        else:
            payload = b'{"id":"msg_t","type":"message","content":[{"type":"text","text":"ok"}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)


@pytest.fixture
def proxy(monkeypatch, tmp_path):
    """가짜 업스트림 + 실제 조립(_make_server) 프록시. (proxy_httpd, ca_pem) 반환."""
    _isolate(monkeypatch, tmp_path)
    from periscribe import apiproxy, proxycert, proxyguard

    up = ThreadingHTTPServer(("127.0.0.1", UP_PORT), _Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    # 프록시의 업스트림 호출(http.client.HTTPSConnection(api.anthropic.com,443))만 로컬 가짜로 우회.
    # 주의: http.client.HTTPSConnection.__init__ 이 super(HTTPSConnection, self) 로 "모듈 전역 이름"을
    # 참조하므로, 이 전역을 패치하면 *실제* HTTPSConnection 생성이 전부 깨진다. 그래서 테스트 클라이언트와
    # health 체크는 http.client 를 쓰지 않고 raw TLS 소켓(_raw_request/_health)으로 친다.
    monkeypatch.setattr(http.client, "HTTPSConnection",
                        lambda host, port=443, timeout=None, context=None:
                        http.client.HTTPConnection("127.0.0.1", UP_PORT, timeout=timeout))

    certs = proxycert.ensure_certs(proxyguard.data_dir())
    httpd = apiproxy._make_server("testhost", PROXY_PORT, str(tmp_path / "spool.jsonl"),
                                  str(tmp_path / "policy.json"),
                                  certs["server_pem"], certs["server_key"])
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd, certs["ca_pem"]
    httpd.shutdown()
    up.shutdown()


def _tls(ca_pem: str, timeout: float):
    """프록시로 가는 raw TLS 소켓. (http.client 미사용 — 위 fixture 주석 참고.)"""
    ctx = ssl.create_default_context(cafile=ca_pem)
    raw = socket.create_connection(("127.0.0.1", PROXY_PORT), timeout=timeout)
    return ctx.wrap_socket(raw, server_hostname="127.0.0.1")


def _recv_all(s) -> bytes:
    data = b""
    while True:
        try:
            chunk = s.recv(4096)
        except (TimeoutError, OSError):
            break
        if not chunk:
            break
        data += chunk
    return data


def _status_of(data: bytes) -> int:
    try:
        return int(data.split(b" ", 2)[1])
    except (IndexError, ValueError):
        return 0


def _request(ca_pem: str, stream: bool = False, timeout: float = 15.0) -> tuple[int, bytes]:
    body = json.dumps({"model": "m", "stream": stream,
                       "metadata": {"user_id": '{"session_id":"t-sess"}'},
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = (f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
           f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body
    s = _tls(ca_pem, timeout)
    try:
        s.sendall(req)
        data = _recv_all(s)            # 프록시가 Connection: close → FIN 까지 읽으면 끝
        return _status_of(data), data
    finally:
        s.close()


def _health(ca_pem: str, timeout: float = 2.0) -> bool:
    from periscribe import proxyguard
    req = (f"GET {proxyguard.HEALTH_PATH} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n").encode()
    try:
        s = _tls(ca_pem, timeout)
    except (ssl.SSLError, OSError, TimeoutError):
        return False
    try:
        s.sendall(req)
        data = _recv_all(s)
        return _status_of(data) == 200 and b"ok" in data.rsplit(b"\r\n\r\n", 1)[-1]
    finally:
        s.close()


def test_32_concurrent_requests_all_succeed(proxy):
    """동시 32 연결 전부 성공. 과거 조립(backlog 5)이면 ECONNREFUSED 다발로 실패하는 테스트."""
    _, ca = proxy
    n = 32
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def worker(i: int):
        barrier.wait()
        try:
            status, _ = _request(ca)
            results[i] = status
        except Exception as e:  # noqa: BLE001
            results[i] = repr(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30.0)
    failed = [r for r in results if r != 200]
    assert not failed, f"{len(failed)}/{n} 실패: {failed[:5]}"


def test_half_open_connection_does_not_block(proxy):
    """TLS 핸드셰이크 없이 TCP 만 열어둔 반열림 커넥션이 있어도 다른 트래픽이 통한다.
    과거 조립(accept 루프 내 핸드셰이크)이면 health 가 타임아웃으로 실패."""
    _, ca = proxy
    stall = socket.create_connection(("127.0.0.1", PROXY_PORT), timeout=5.0)
    try:
        time.sleep(0.3)
        assert _health(ca, timeout=2.0), "반열림 커넥션 1개가 프록시 전체를 블록함"
        status, _ = _request(ca)
        assert status == 200
    finally:
        stall.close()


def test_concurrent_streams_with_health_probes(proxy):
    """SSE 스트리밍 동시 4개가 흐르는 중에도 health 프로브(guardian 모사)가 성공한다."""
    _, ca = proxy
    errors: list = []

    def stream_worker():
        try:
            status, body = _request(ca, stream=True)
            if status != 200 or b"message_stop" not in body:
                errors.append(f"stream status={status}")
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=stream_worker) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.2)  # 스트림이 흐르는 중간에 프로브
    for _ in range(3):
        if not _health(ca, timeout=2.0):
            errors.append("health probe failed during streams")
        time.sleep(0.1)
    for t in threads:
        t.join(timeout=15.0)
    assert not errors, errors


def test_aborted_stream_then_next_request_ok(proxy):
    """스트리밍 중 클라이언트가 끊어도(사용자 Esc 모사) 프록시는 다음 요청을 정상 처리한다."""
    _, ca = proxy
    body = json.dumps({"model": "m", "stream": True,
                       "metadata": {"user_id": '{"session_id":"t-sess"}'},
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = (f"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
           f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body
    s = _tls(ca, 10.0)
    s.sendall(req)
    s.recv(64)      # 일부만 읽고
    s.close()       # 중도 절단
    status, _ = _request(ca)
    assert status == 200
