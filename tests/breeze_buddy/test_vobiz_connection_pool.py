"""Vobiz dials reuse one keep-alive connection pool per process.

make_call used ``requests.post``, which builds and closes a session per call,
so every dial opened its own TCP + TLS connection to the Vobiz API. Now each
dial still gets its own session (no cookie or state carries from one dial to
the next) but every session sends over one shared pool. The account rides each
request (``X-Auth-*`` headers and the URL path), so the pool serves any account.

The dial tests run make_call against a local HTTPS server that counts TLS
connections and records each request's account. Nothing here reaches Vobiz.
"""

import datetime
import ipaddress
import json
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, cast

import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from requests.adapters import HTTPAdapter

import app.ai.voice.agents.breeze_buddy.services.telephony.vobiz.vobiz as vz
from app.ai.voice.agents.breeze_buddy.services.telephony.vobiz.vobiz import (
    VobizProvider,
)
from app.core.config import static

CUSTOMER = "+919000000001"
NUMBER = "+918000000001"
ACCOUNTS = [(f"MAVOBIZ{c}", f"token-{c}") for c in "ABC"]


def _self_signed(tmp: Path) -> Tuple[Path, Path]:
    """A throwaway certificate for 127.0.0.1 that is its own trust anchor."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp / "cert.pem", tmp / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class _Vobiz(ThreadingHTTPServer):
    """Answers make-call like Vobiz; counts TLS connections and records, per
    request, (connection, X-Auth-ID, account in the URL path, Cookie sent)."""

    daemon_threads = True

    def __init__(self, ctx: ssl.SSLContext, mode: str) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.socket = ctx.wrap_socket(
            self.socket, server_side=True, do_handshake_on_connect=False
        )
        self.mode = mode  # "ok" | "close" | "redirect" | "cookie"
        self.lock = threading.Lock()
        self.connections = 0
        self.seen: List[Tuple[int, str, str, Optional[str]]] = []

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # a client that hung up mid-test is not a failure here


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, as Vobiz's API

    def setup(self) -> None:
        self.request.do_handshake()
        super().setup()
        srv = cast(_Vobiz, self.server)
        with srv.lock:
            srv.connections += 1
            self.connection_no = srv.connections

    def do_POST(self) -> None:
        srv = cast(_Vobiz, self.server)
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        auth_id = str(self.headers.get("X-Auth-ID", ""))
        parts = self.path.split("/")  # /api/v1/Account/{auth_id}/Call/
        in_path = parts[4] if len(parts) > 4 else ""
        with srv.lock:
            srv.seen.append(
                (self.connection_no, auth_id, in_path, self.headers.get("Cookie"))
            )
            n = len(srv.seen)
        if srv.mode == "redirect" and "/Call/" in self.path:
            self.send_response(302)
            self.send_header("Location", "/elsewhere/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(
            {
                "api_id": f"a-{n}",
                "message": "Call fired",
                "request_uuid": f"{auth_id}:{n}",
            }
        ).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if srv.mode == "cookie":
            self.send_header("Set-Cookie", "edge=sticky; Path=/")
        self.end_headers()
        self.wfile.write(body)
        # Close without "Connection: close", as a server dropping an idle
        # keep-alive connection does: the client still holds it as reusable.
        self.close_connection = srv.mode == "close"

    # A followed redirect arrives as a GET: answer and record it the same way.
    do_GET = do_POST

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture(autouse=True)
def vobiz_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vz, "APP_BASE_URL", "https://bb.example.test")
    monkeypatch.setattr(vz, "get_proxy_config", lambda: None)
    _use(monkeypatch, ACCOUNTS[0])
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")


def _use(monkeypatch: pytest.MonkeyPatch, account: Tuple[str, str]) -> None:
    """The Vobiz account dials sign with (one per environment today)."""
    monkeypatch.setattr(vz, "VOBIZ_AUTH_ID", account[0])
    monkeypatch.setattr(vz, "VOBIZ_AUTH_TOKEN", account[1])


@pytest.fixture
def serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., _Vobiz]]:
    """Start a local Vobiz on HTTPS, point the provider at it, and give the
    test a fresh pool so connection counts are its own."""
    cert, key = _self_signed(tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert))
    monkeypatch.setattr(
        vz, "_VOBIZ_POOL", HTTPAdapter(pool_maxsize=static.BLOCKING_THREAD_POOL_SIZE)
    )
    servers: List[_Vobiz] = []

    def start(mode: str = "ok") -> _Vobiz:
        server = _Vobiz(ctx, mode)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        monkeypatch.setattr(
            vz, "VOBIZ_API_BASE_URL", f"https://127.0.0.1:{port}/api/v1"
        )
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _dial() -> Dict[str, Any]:
    """Dial once, as the dispatch worker does (a provider per dial)."""
    call = VobizProvider(aiohttp_session=None).make_call(CUSTOMER, NUMBER)
    assert call is not None
    return call


def test_each_dial_gets_its_own_session_over_the_one_pool() -> None:
    first, second = vz._session(), vz._session()
    assert first is not second
    assert first.cookies is not second.cookies
    for session in (first, second):
        assert session.get_adapter("https://api.vobiz.ai/") is vz._VOBIZ_POOL
    assert vz._VOBIZ_TIMEOUT == (5, 30)


def test_without_the_pool_every_dial_opens_its_own_tls_connection(
    serve: Callable[..., _Vobiz], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release behaviour, kept as the baseline the counter is checked on."""
    server = serve()
    monkeypatch.setattr(vz, "_session", requests.Session)
    for _ in range(6):
        assert _dial()["status"] == "call_initiated"
    assert server.connections == 6


def test_back_to_back_dials_reuse_one_connection_whatever_account_signs_them(
    serve: Callable[..., _Vobiz], monkeypatch: pytest.MonkeyPatch
) -> None:
    server = serve()
    calls = []
    for i in range(6):
        _use(monkeypatch, ACCOUNTS[i % 3])
        calls.append(_dial())
    expected = [ACCOUNTS[i % 3][0] for i in range(6)]
    assert [c["sid"].split(":")[0] for c in calls] == expected
    assert server.connections == 1
    assert [auth_id for _, auth_id, _, _ in server.seen] == expected
    assert all(auth_id == in_path for _, auth_id, in_path, _ in server.seen)


def test_concurrent_dials_share_few_connections(
    serve: Callable[..., _Vobiz],
) -> None:
    server = serve()
    workers, dials = 12, 60
    with ThreadPoolExecutor(workers) as threads:
        calls = list(threads.map(lambda _: _dial(), range(dials)))
    assert all(c["status"] == "call_initiated" for c in calls)
    assert len({c["sid"] for c in calls}) == dials
    assert len(server.seen) == dials
    assert all(auth_id == ACCOUNTS[0][0] for _, auth_id, _, _ in server.seen)
    assert server.connections <= workers


def test_a_connection_vobiz_closed_is_replaced_not_reused(
    serve: Callable[..., _Vobiz],
) -> None:
    server = serve("close")
    for _ in range(5):
        time.sleep(0.05)  # the close lands while the connection sits idle in the pool
        assert _dial()["status"] == "call_initiated"
    assert server.connections == 5
    assert len(server.seen) == 5


def test_a_redirect_is_still_not_followed_over_the_pool(
    serve: Callable[..., _Vobiz],
) -> None:
    """A followed redirect would carry X-Auth-* to wherever it points."""
    server = serve("redirect")
    assert VobizProvider(aiohttp_session=None).make_call(CUSTOMER, NUMBER) is None
    assert len(server.seen) == 1


def test_no_cookie_carries_from_one_dial_to_the_next(
    serve: Callable[..., _Vobiz],
) -> None:
    server = serve("cookie")
    for _ in range(3):
        assert _dial()["status"] == "call_initiated"
    assert server.connections == 1
    assert [cookie for _, _, _, cookie in server.seen] == [None, None, None]


def test_a_stalled_tls_handshake_frees_the_dial_thread_at_the_connect_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TCP connects (the kernel accepts) but TLS never answers: the thread is
    freed after the connect cap, not the 30 s read timeout."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        port = listener.getsockname()[1]
        base = f"https://127.0.0.1:{port}/api/v1"
        monkeypatch.setattr(vz, "VOBIZ_API_BASE_URL", base)
        monkeypatch.setattr(vz, "_VOBIZ_TIMEOUT", (0.5, 5))
        started = time.monotonic()
        with pytest.raises(requests.exceptions.ReadTimeout, match=r"read timeout=0\.5"):
            vz._session().post(f"{base}/Account/x/Call/", timeout=vz._VOBIZ_TIMEOUT)
        assert time.monotonic() - started < 2.5
        assert VobizProvider(aiohttp_session=None).make_call(CUSTOMER, NUMBER) is None
    finally:
        listener.close()
