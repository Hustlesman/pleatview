"""The master key, tenant keys and the console's own TLS certificate.

Each tenant has a random 256-bit key, stored wrapped by the master key with AES-256-GCM.
Secrets in a tenant's database are sealed with the tenant's key, so deleting the tenant
(and its wrapped key) leaves old copies of those secrets unreadable.
"""

from __future__ import annotations

import base64
import datetime
import ipaddress
import os
from pathlib import Path
import secrets
import socket

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID

KEY_BYTES = 32
NONCE_BYTES = 12


def write_private(path: Path, data: bytes) -> None:
    """Writes a file only the console's user can read, never leaving it readable on the way."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def load_master_key(path: Path) -> bytes:
    """Reads the master key, making one on first start."""
    if not path.exists():
        write_private(path, base64.b64encode(secrets.token_bytes(KEY_BYTES)) + b"\n")
    key = base64.b64decode(path.read_bytes().strip(), validate=True)
    if len(key) != KEY_BYTES:
        raise ValueError(f"{path} does not hold a 256-bit key")
    return key


def _seal(key: bytes, plain: bytes, aad: str) -> str:
    nonce = secrets.token_bytes(NONCE_BYTES)
    return base64.b64encode(nonce + AESGCM(key).encrypt(nonce, plain, aad.encode())).decode()


def _open(key: bytes, sealed: str, aad: str) -> bytes:
    raw = base64.b64decode(sealed, validate=True)
    return AESGCM(key).decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], aad.encode())


def seal(key: bytes, text: str, context: str) -> str:
    """Seals a console-level secret (such as a two-factor secret) with the master key."""
    return _seal(key, text.encode(), context)


def open_sealed(key: bytes, sealed: str, context: str) -> str:
    return _open(key, sealed, context).decode()


def new_tenant_key(master: bytes, tenant_id: str) -> str:
    """A new tenant key, wrapped by the master key and bound to the tenant's id."""
    return _seal(master, secrets.token_bytes(KEY_BYTES), f"tenant:{tenant_id}")


class Sealer:
    """Seals and opens a tenant's secrets. The context string binds each secret to its place,
    so a sealed value copied to another row or tenant doesn't open."""

    def __init__(self, master: bytes, tenant_id: str, wrapped: str) -> None:
        self._key = _open(master, wrapped, f"tenant:{tenant_id}")
        self._tenant = tenant_id

    def seal(self, text: str, context: str) -> str:
        return _seal(self._key, text.encode(), f"{self._tenant}:{context}")

    def open(self, sealed: str, context: str) -> str:
        return _open(self._key, sealed, f"{self._tenant}:{context}").decode()


def ensure_certificate(cert: Path, key: Path) -> None:
    """Makes a self-signed certificate for this machine's name and addresses, once."""
    if cert.exists() and key.exists():
        return
    private = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname()
    names: list[x509.GeneralName] = [x509.DNSName(host), x509.DNSName("localhost")]
    if "." not in host:
        names.append(x509.DNSName(f"{host}.local"))
    for address in local_addresses():
        names.append(x509.IPAddress(ipaddress.ip_address(address)))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"PleatView Console on {host}"[:64])])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(private, hashes.SHA256())
    )
    write_private(
        key,
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    write_private(cert, certificate.public_bytes(serialization.Encoding.PEM))


def fingerprint(cert: Path) -> str:
    """The certificate's SHA-256 fingerprint, as browsers show it."""
    certificate = x509.load_pem_x509_certificate(cert.read_bytes())
    return certificate.fingerprint(hashes.SHA256()).hex(":").upper()


def local_addresses() -> list[str]:
    """This machine's IPv4 addresses, as far as the standard library can tell."""
    found = {"127.0.0.1"}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))  # no packet is sent; only picks the outgoing address
            found.add(probe.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    return sorted(found)
