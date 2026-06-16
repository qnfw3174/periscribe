"""proxycert — 로컬 API 프록시용 자체 CA + 127.0.0.1 리프 인증서 생성/보관.

Claude(Node)는 NODE_EXTRA_CA_CERTS 로 지정한 CA 를 신뢰하므로, 우리 CA 가 서명한
127.0.0.1 서버 인증서로 TLS 를 종료해 트래픽을 복호화할 수 있다. Windows 인증서 저장소
설치 불필요(파일만). 무관리자(사용자 프로필에 기록). `cryptography`(기존 의존성) 사용.
"""

from __future__ import annotations

import datetime
import ipaddress
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

# new_date 류는 워크플로우/캐시 환경에서 막힐 수 있으나, 여기는 실제 실행 프로그램이라 datetime 직접 사용.
_DAY = datetime.timedelta(days=1)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _gen_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _write_key(p: Path, key: rsa.RSAPrivateKey) -> None:
    p.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))


def _write_cert(p: Path, cert: x509.Certificate) -> None:
    p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _gen_ca(ca_pem: Path, ca_key: Path) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = _gen_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Periscribe Local CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - _DAY)
        .not_valid_after(_now() + 3650 * _DAY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=False, content_commitment=False, key_encipherment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=True,
            crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
        .sign(key, hashes.SHA256())
    )
    _write_cert(ca_pem, cert)
    _write_key(ca_key, key)
    return cert, key


def _gen_leaf(server_pem: Path, server_key: Path,
              ca_cert: x509.Certificate, ca_key: rsa.RSAPrivateKey) -> None:
    key = _gen_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    san = x509.SubjectAlternativeName([
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
        x509.DNSName("localhost"),
        # 컨테이너(agent --proxy)가 host.docker.internal:<port> 로 호스트 프록시에 접속할 때 검증 통과용.
        x509.DNSName("host.docker.internal"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_now() - _DAY)
        .not_valid_after(_now() + 825 * _DAY)
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_cert(server_pem, cert)
    _write_key(server_key, key)


def _leaf_valid(server_pem: Path) -> bool:
    try:
        cert = x509.load_pem_x509_certificate(server_pem.read_bytes())
        if cert.not_valid_after_utc <= _now() + 30 * _DAY:
            return False
        # host.docker.internal SAN 이 없으면(구버전 인증서) 재발급 대상 — 컨테이너 프록시 접속 위해 필요.
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return "host.docker.internal" in san.get_values_for_type(x509.DNSName)
    except Exception:
        return False


def ensure_certs(data_dir: Path) -> dict[str, Any]:
    """CA + 리프(127.0.0.1/localhost/host.docker.internal)를 보장하고 경로 dict 반환.
    CA 가 있으면 재사용(NODE_EXTRA_CA_CERTS 신뢰 유지)하고 리프만 필요 시 재발급. 반환: {ca_pem, server_pem, server_key}."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    ca_pem = data_dir / "ca.pem"
    ca_key = data_dir / "ca.key"
    server_pem = data_dir / "server.pem"
    server_key = data_dir / "server.key"

    if not (ca_pem.is_file() and ca_key.is_file()):
        # CA 없음 → CA + 리프 새로 생성.
        ca_cert, ca_priv = _gen_ca(ca_pem, ca_key)
        _gen_leaf(server_pem, server_key, ca_cert, ca_priv)
    elif not (server_pem.is_file() and server_key.is_file() and _leaf_valid(server_pem)):
        # CA 는 유지(신뢰 보존), 리프만 재발급(만료/SAN 누락).
        ca_cert = x509.load_pem_x509_certificate(ca_pem.read_bytes())
        ca_priv = serialization.load_pem_private_key(ca_key.read_bytes(), password=None)
        _gen_leaf(server_pem, server_key, ca_cert, ca_priv)
    return {
        "ca_pem": str(ca_pem.resolve()),
        "server_pem": str(server_pem.resolve()),
        "server_key": str(server_key.resolve()),
    }
