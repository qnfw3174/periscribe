"""E2EE — payload 암호화(클라이언트 측).

설계(plan/E2EE-DESIGN): 컬렉터는 머신마다 랜덤 per-device DEK(AES-256)를 로컬 생성해
payload/raw 를 AES-256-GCM 으로 암호화한다. 그 DEK 자체는 owner 공개키(RSA-OAEP)로 봉인해
서버에 올린다 → 운영자/DB는 암호문만 본다(제로지식). 평문 DEK·패스프레이즈·개인키는
서버를 절대 통과하지 않는다(개인키 복원·복호는 웹 관리자 측에서만).

웹(WebCrypto)과 호환되도록:
- RSA-OAEP, MGF1+SHA-256, label 없음 (WebCrypto RSA-OAEP 기본과 일치).
- AES-256-GCM, 96-bit(12B) nonce, 태그 포함 ciphertext. envelope {v,kid,n,ct}(base64).
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENVELOPE_VERSION = 1
DEK_BYTES = 32  # AES-256


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def gen_dek() -> bytes:
    """머신별 랜덤 DEK(AES-256). 패스프레이즈 불필요."""
    return os.urandom(DEK_BYTES)


def dek_to_b64(dek: bytes) -> str:
    return _b64(dek)


def dek_from_b64(s: str) -> bytes:
    return base64.b64decode(s)


def wrap_dek_rsa(public_key_spki_b64: str, dek: bytes) -> str:
    """owner 공개키(SPKI base64)로 DEK 봉인(RSA-OAEP-SHA256). 결과는 base64."""
    der = base64.b64decode(public_key_spki_b64)
    pub = serialization.load_der_public_key(der)
    ct = pub.encrypt(
        dek,
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return _b64(ct)


def encrypt_field(dek: bytes, obj: Any, kid: int = 1) -> dict[str, Any]:
    """JSON 직렬화 가능한 값을 AES-256-GCM 으로 암호화 → envelope dict."""
    nonce = os.urandom(12)
    pt = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
    ct = AESGCM(dek).encrypt(nonce, pt, None)
    return {"v": ENVELOPE_VERSION, "kid": kid, "n": _b64(nonce), "ct": _b64(ct)}
