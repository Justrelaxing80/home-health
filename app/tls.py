"""Built-in HTTPS, so phones get the live camera barcode scanner.

Browsers only allow live camera access on secure (https) pages, and the app is
usually opened at a plain http address such as a Tailscale IP. So the app makes
its own small certificate authority (once, kept in DATA_DIR/tls) and issues a
server certificate covering every address it is reached at. serve.py answers
http and https on the same port.

A phone shows a warning for these certificates until the CA is installed on it
(GET /api/tls/ca.crt). The CA is name-constrained to private, Tailscale and
loopback IP ranges plus local hostnames (.local, .lan, .ts.net, ...), so even
when installed it can't vouch for any public website.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import ssl
import threading

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from . import db

log = logging.getLogger("home-health.tls")

ACTIVE = False  # set by serve.py when https is being served

PERMITTED_NETS = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "fc00::/7", "fe80::/10", "::1/128")]
PERMITTED_DNS = ["localhost", "local", "lan", "home", "internal", "home.arpa", "ts.net", "localdomain"]
MAX_HOSTS = 40
LEAF_DAYS = 397  # Apple's limit for server certificates
RENEW_DAYS = 30

_lock = threading.RLock()
_ctx: ssl.SSLContext | None = None  # the listening context
_fresh: ssl.SSLContext | None = None  # newest certificate, for a handshake already in progress
_hosts: list[str] = []
_leaf_expires: dt.datetime | None = None
_HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


def _dir():
    d = db.DATA_DIR / "tls"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(path, data: bytes, private=False):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    if private:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _env_hosts() -> list[str]:
    return [h for h in re.split(r"[\s,]+", os.environ.get("HTTPS_HOSTS", "")) if h]


def _dns_suffixes() -> list[str]:
    # Hostnames named in HTTPS_HOSTS when the CA is created are allowed too (e.g. a bare "zimaos").
    extra = [h.lower().rstrip(".") for h in _env_hosts() if _parse_ip(h) is None and _HOST_RE.match(h.lower())]
    return list(dict.fromkeys(PERMITTED_DNS + extra))


def _parse_ip(h: str):
    try:
        return ipaddress.ip_address(h.strip("[]").split("%")[0])
    except ValueError:
        return None


def clean_host(raw: str) -> str | None:
    """'100.101.1.2:9085' -> '100.101.1.2'; '[fd7a::1]:9085' -> 'fd7a::1'; None if not one we can certify."""
    h = (raw or "").strip().lower()
    if h.startswith("["):
        h = h[1:h.find("]")] if "]" in h else ""
    elif h.count(":") == 1:
        h = h.split(":")[0]
    h = h.rstrip(".")
    ip = _parse_ip(h)
    if ip is not None:
        return str(ip) if any(ip in n for n in PERMITTED_NETS if n.version == ip.version) else None
    if not _HOST_RE.match(h):
        return None
    sfx = _CA_SUFFIXES or _dns_suffixes()
    return h if any(h == s or h.endswith("." + s) for s in sfx) else None


_CA_SUFFIXES: list[str] = []


def _load_or_make_ca():
    global _CA_SUFFIXES
    d = _dir()
    kp, cp = d / "ca.key", d / "ca.crt"
    if kp.exists() and cp.exists():
        key = serialization.load_pem_private_key(kp.read_bytes(), None)
        cert = x509.load_pem_x509_certificate(cp.read_bytes())
        try:
            nc = cert.extensions.get_extension_for_class(x509.NameConstraints).value
            _CA_SUFFIXES = [g.value for g in nc.permitted_subtrees or [] if isinstance(g, x509.DNSName)]
        except x509.ExtensionNotFound:
            _CA_SUFFIXES = _dns_suffixes()
        return key, cert
    _CA_SUFFIXES = _dns_suffixes()
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Home Health"),
                      x509.NameAttribute(NameOID.COMMON_NAME, f"Home Health local CA {secrets.token_hex(3)}")])
    now = dt.datetime.now(dt.timezone.utc)
    permitted = [x509.IPAddress(n) for n in PERMITTED_NETS] + [x509.DNSName(s) for s in _CA_SUFFIXES]
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.NameConstraints(permitted_subtrees=permitted, excluded_subtrees=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    _write(kp, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()), private=True)
    _write(cp, cert.public_bytes(serialization.Encoding.PEM))
    log.info("created local certificate authority")
    return key, cert


def _issue_leaf(ca_key, ca_cert, hosts: list[str]):
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    sans = [x509.IPAddress(ipaddress.ip_address(h)) if _parse_ip(h) else x509.DNSName(h) for h in hosts]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Home Health")]))
            .issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(hours=1)).not_valid_after(now + dt.timedelta(days=LEAF_DAYS))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=False, crl_sign=False,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


def _rebuild():
    """(Re)issue the server certificate for the current host list and load it into the live context."""
    global _leaf_expires, _fresh
    ca_key, ca_cert = _load_or_make_ca()
    key, cert = _issue_leaf(ca_key, ca_cert, _hosts)
    d = _dir()
    _write(d / "server.key", key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()), private=True)
    _write(d / "server.crt", cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM))
    _write(d / "hosts.json", json.dumps(_hosts).encode())
    _leaf_expires = cert.not_valid_after_utc
    if _ctx is not None:
        _ctx.load_cert_chain(d / "server.crt", d / "server.key")  # new connections get the new certificate
        _fresh = _plain_context()


def _load_existing() -> bool:
    global _leaf_expires
    d = _dir()
    try:
        cert = x509.load_pem_x509_certificate((d / "server.crt").read_bytes())
        saved = json.loads((d / "hosts.json").read_text())
    except (OSError, ValueError):
        return False
    if not (d / "server.key").exists():
        return False
    _hosts[:] = [h for h in saved if clean_host(h) == h][:MAX_HOSTS]
    _leaf_expires = cert.not_valid_after_utc
    return set(_hosts) == set(saved)


def server_context() -> ssl.SSLContext:
    """The TLS context serve.py listens with. Certificates update in place as new hosts show up."""
    global _ctx
    with _lock:
        _load_or_make_ca()
        fresh = _load_existing()
        want = list(dict.fromkeys(["localhost", "127.0.0.1"] + [h for h in map(clean_host, _env_hosts()) if h] + _hosts))
        if not fresh or want != _hosts or _needs_renewal():
            _hosts[:] = want[:MAX_HOSTS]
            _rebuild()
        ctx = _plain_context()

        def on_sni(sslobj, server_name, _context):
            if server_name and note_host(server_name) and _fresh is not None:
                sslobj.context = _fresh  # this handshake already gets the new certificate
        ctx.sni_callback = on_sni
        _ctx = ctx
        return ctx


def _plain_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_alpn_protocols(["http/1.1"])
    d = _dir()
    ctx.load_cert_chain(d / "server.crt", d / "server.key")
    return ctx


def _needs_renewal() -> bool:
    return _leaf_expires is None or _leaf_expires - dt.datetime.now(dt.timezone.utc) < dt.timedelta(days=RENEW_DAYS)


def note_host(raw: str) -> bool:
    """Add an address the app was reached at to the certificate. Returns True if the certificate changed."""
    if _ctx is None:
        return False
    h = clean_host(raw)
    renew = _needs_renewal()
    if not renew and (h is None or h in _hosts):
        return False
    with _lock:
        if h and h not in _hosts:
            if len(_hosts) >= MAX_HOSTS:
                return False
            _hosts.append(h)
            log.info("adding %s to the https certificate", h)
        elif not _needs_renewal():
            return False
        try:
            _rebuild()
        except Exception:  # noqa: BLE001
            log.exception("couldn't update the https certificate")
            return False
        return True


def ca_der() -> bytes:
    return x509.load_pem_x509_certificate((_dir() / "ca.crt").read_bytes()).public_bytes(serialization.Encoding.DER)


def info() -> dict:
    if not ACTIVE:
        return {"enabled": False}
    der = ca_der()
    fp = hashlib.sha256(der).hexdigest().upper()
    return {"enabled": True, "hosts": list(_hosts), "ca_fingerprint": ":".join(fp[i:i + 2] for i in range(0, 64, 2)),
            "expires": _leaf_expires.date().isoformat() if _leaf_expires else None}
