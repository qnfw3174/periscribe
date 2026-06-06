"""E2EE 암호화 테스트: DEK 생성/봉인, payload 암호화, sink emit 암호화 패스."""

import base64
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from periscribe import crypto
from periscribe.sink import IngestSink


def _owner_keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    spki_b64 = base64.b64encode(priv.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    return priv, spki_b64


def test_gen_dek_length():
    assert len(crypto.gen_dek()) == 32


def test_encrypt_field_envelope_and_roundtrip():
    dek = crypto.gen_dek()
    env = crypto.encrypt_field(dek, {"command": "ls -la", "n": 1}, kid=2)
    assert set(env.keys()) == {"v", "kid", "n", "ct"}
    assert env["kid"] == 2
    # AES-GCM 복호화로 원문 복원
    pt = AESGCM(dek).decrypt(base64.b64decode(env["n"]), base64.b64decode(env["ct"]), None)
    assert json.loads(pt) == {"command": "ls -la", "n": 1}


def test_wrap_dek_rsa_unwraps_with_private_key():
    priv, spki = _owner_keypair()
    dek = crypto.gen_dek()
    wrapped = crypto.wrap_dek_rsa(spki, dek)
    recovered = priv.decrypt(
        base64.b64decode(wrapped),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    assert recovered == dek


def test_sink_emit_encrypts_payload_and_raw():
    priv, spki = _owner_keypair()
    dek = crypto.gen_dek()
    sink = IngestSink("http://x/ingest", "tok", dek=dek, dek_kid=1)
    sink.set_public_key(spki)
    posted = {}

    def fake_post(rows):
        posted["rows"] = rows
        return {}

    sink._post = fake_post  # type: ignore[assignment]
    sink.emit([{"event_id": "a", "kind": "tool_use", "tool": "Bash",
                "payload": {"command": "rm -rf /"}, "raw": {"line": "x"}}])

    row = posted["rows"][0]
    # 메타데이터는 평문
    assert row["event_id"] == "a" and row["kind"] == "tool_use" and row["tool"] == "Bash"
    assert row["enc_version"] == 1
    # payload/raw 는 envelope 암호문(평문 노출 X)
    assert set(row["payload"].keys()) == {"v", "kid", "n", "ct"}
    assert "rm -rf" not in json.dumps(row["payload"])
    pt = AESGCM(dek).decrypt(base64.b64decode(row["payload"]["n"]),
                             base64.b64decode(row["payload"]["ct"]), None)
    assert json.loads(pt) == {"command": "rm -rf /"}
    # 봉인된 DEK가 하트비트(machine)에 실렸는지
    assert sink.machine.get("wrapped_dek")
    assert priv.decrypt(base64.b64decode(sink.machine["wrapped_dek"]),
                        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                     algorithm=hashes.SHA256(), label=None)) == dek


def test_sink_no_dek_leaves_plaintext():
    sink = IngestSink("http://x/ingest", "tok")  # dek 없음
    posted = {}
    sink._post = lambda rows: posted.setdefault("rows", rows) or {}  # type: ignore[assignment]
    sink.emit([{"event_id": "a", "payload": {"command": "ls"}}])
    row = posted["rows"][0]
    assert row["payload"] == {"command": "ls"}
    assert "enc_version" not in row
