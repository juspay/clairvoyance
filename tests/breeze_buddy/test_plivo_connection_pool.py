"""Plivo REST calls reuse one keep-alive connection pool per process.

Every dial builds its own ``plivo.RestClient``: the SDK keeps per-request state
on it (``base_uri``, ``voice_retry_count``), so a client is never shared. Before
this each client also opened its own TCP + TLS connection to api.plivo.com. Now
every client, whatever its account, sends over one shared pool: the account rides
each request (Basic auth and the URL path), so a socket last used by one account
carries the next account's request as that account.

The dial tests run the real SDK against a local HTTPS server that counts TLS
connections and records the account each request was signed with. Nothing here
reaches Plivo.
"""

import asyncio
import base64
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
from typing import Any, Callable, Dict, Iterator, List, Set, Tuple, cast

import plivo.rest.client as sdk
import pytest
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from requests.adapters import HTTPAdapter

import app.ai.voice.agents.breeze_buddy.services.telephony.plivo.plivo as plivo_mod
from app.ai.voice.agents.breeze_buddy.accounts import PlivoAccount
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo.plivo import (
    PlivoProvider,
)
from app.core.config import static
from app.core.config.static import PLIVO_REST_TIMEOUT_SECONDS

CUSTOMER = "+919000000001"
NUMBER = "+918000000001"
ACCOUNTS = [
    PlivoAccount(auth_id=f"MAPOOLTEST{c}000000001", auth_token=f"token-{c}")
    for c in "ABC"
]


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


class _Plivo(ThreadingHTTPServer):
    """Answers make-call like Plivo; counts TLS connections and records, per
    request, (connection, account it was signed as, account in its URL path)."""

    daemon_threads = True

    def __init__(self, ctx: ssl.SSLContext, close_after_reply: bool) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.socket = ctx.wrap_socket(
            self.socket, server_side=True, do_handshake_on_connect=False
        )
        self.close_after_reply = close_after_reply
        self.lock = threading.Lock()
        self.connections = 0
        self.seen: List[Tuple[int, str, str]] = []

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # a client that hung up mid-test is not a failure here


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, as Plivo's API

    def setup(self) -> None:
        self.request.do_handshake()
        super().setup()
        srv = cast(_Plivo, self.server)
        with srv.lock:
            srv.connections += 1
            self.connection_no = srv.connections

    def do_POST(self) -> None:
        srv = cast(_Plivo, self.server)
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        signed_as = base64.b64decode(
            str(self.headers.get("Authorization", "")).split()[1]
        )
        signed_as_id = signed_as.decode().split(":")[0]
        in_path = self.path.split("/")[3]  # /v1/Account/{auth_id}/Call/
        with srv.lock:
            srv.seen.append((self.connection_no, signed_as_id, in_path))
            n = len(srv.seen)
        body = json.dumps(
            {
                "api_id": f"a-{n}",
                "message": "call fired",
                "request_uuid": f"{signed_as_id}:{n}",
            }
        ).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        # Close without "Connection: close", as a server dropping an idle
        # keep-alive connection does: the client still holds it as reusable.
        self.close_connection = srv.close_after_reply

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture(autouse=True)
def env_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(static, "PLIVO_AUTH_ID", "MAENVTEST00000000001")
    monkeypatch.setattr(static, "PLIVO_AUTH_TOKEN", "env-token")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")


@pytest.fixture
def serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., _Plivo]]:
    """Start a local Plivo on HTTPS, point the SDK's voice API at it, and give
    the test a fresh pool so connection counts are its own."""
    cert, key = _self_signed(tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)

    # The SDK calls Session.send(), which never reads REQUESTS_CA_BUNDLE: trust
    # the test certificate on the sessions the SDK builds instead.
    class TrustingSession(requests.Session):
        def __init__(self) -> None:
            super().__init__()
            self.verify = str(cert)

    monkeypatch.setattr(sdk, "Session", TrustingSession)
    monkeypatch.setattr(
        plivo_mod,
        "_PLIVO_POOL",
        HTTPAdapter(pool_maxsize=static.BLOCKING_THREAD_POOL_SIZE),
    )
    servers: List[_Plivo] = []

    def start(close_after_reply: bool = False) -> _Plivo:
        server = _Plivo(ctx, close_after_reply)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        monkeypatch.setattr(
            sdk, "API_VOICE_BASE_URI", f"https://127.0.0.1:{port}/v1/Account"
        )
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _dialler(account: PlivoAccount) -> PlivoProvider:
    """A provider on ``account``, built per dial as the dispatch worker does."""
    provider = PlivoProvider(aiohttp_session=None)
    provider.APP_BASE_URL = "https://bb.example.test"  # the SDK checks the URL
    provider._use(account)
    return provider


def _dial(account: PlivoAccount) -> Dict[str, Any]:
    """Dial on ``account``; make_call answered (None means it failed)."""
    call = _dialler(account).make_call(CUSTOMER, NUMBER)
    assert call is not None
    return call


def test_every_accounts_client_sends_over_the_one_pool_signed_as_itself() -> None:
    providers = [PlivoProvider(aiohttp_session=None)] + [_dialler(a) for a in ACCOUNTS]
    for provider in providers:
        for session in (provider.client.session, provider.client.multipart_session):
            assert (
                session.get_adapter("https://api.plivo.com/") is plivo_mod._PLIVO_POOL
            )
        assert provider.client.timeout == (5, PLIVO_REST_TIMEOUT_SECONDS)
    assert [p.client.session.auth for p in providers] == [
        ("MAENVTEST00000000001", "env-token")
    ] + [(a.auth_id, a.auth_token) for a in ACCOUNTS]
    # Each call keeps its own client and session; only the sockets are shared.
    assert len({id(p.client.session) for p in providers}) == len(providers)


def test_the_mpc_transfer_client_uses_the_pool_on_the_calls_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clients: List[Any] = []

    class Conference:
        def __init__(self, client: Any) -> None:
            clients.append(client)

        async def move_customer_to_mpc(self, **_kwargs: Any) -> Dict[str, Any]:
            return {"success": True}

    async def lead(_call_sid: str) -> Any:
        return object()

    async def account(_lead: Any) -> PlivoAccount:
        return ACCOUNTS[1]

    async def publish(_channel: str, _payload: Dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(plivo_mod, "get_lead_by_call_id", lead)
    monkeypatch.setattr(plivo_mod, "lead_plivo_account", account)
    monkeypatch.setattr(plivo_mod, "publish_hold_transfer_result", publish)
    monkeypatch.setattr(plivo_mod, "PlivoConferenceService", Conference)
    join = {
        "call_sid": "CA-1",
        "EventName": "ParticipantJoin",
        "MPCName": "transfer-CA-1",
        "ParticipantRole": "agent",
    }
    asyncio.run(plivo_mod.handle_mpc_transfer_webhook(join))
    [client] = clients
    assert client.session.get_adapter("https://api.plivo.com/") is plivo_mod._PLIVO_POOL
    assert client.session.auth == (ACCOUNTS[1].auth_id, ACCOUNTS[1].auth_token)
    assert client.timeout == (5, PLIVO_REST_TIMEOUT_SECONDS)


def test_without_the_pool_every_dial_opens_its_own_tls_connection(
    serve: Callable[..., _Plivo], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release behaviour, kept as the baseline the counter is checked on."""
    server = serve()
    monkeypatch.setattr(plivo_mod, "_pooled", lambda client: client)
    for i in range(6):
        assert _dial(ACCOUNTS[i % 3])["status"] == ("call_initiated")
    assert server.connections == 6


def test_back_to_back_dials_on_different_accounts_reuse_one_connection(
    serve: Callable[..., _Plivo],
) -> None:
    server = serve()
    calls = [_dial(ACCOUNTS[i % 3]) for i in range(6)]
    assert [c["sid"].split(":")[0] for c in calls] == [
        ACCOUNTS[i % 3].auth_id for i in range(6)
    ]
    assert server.connections == 1
    assert [signed for _, signed, _ in server.seen] == [
        ACCOUNTS[i % 3].auth_id for i in range(6)
    ]


def test_concurrent_dials_on_three_accounts_share_few_connections_and_each_keeps_its_account(
    serve: Callable[..., _Plivo],
) -> None:
    server = serve()
    workers, dials = 12, 90

    def dial(i: int) -> Tuple[str, Dict[str, Any]]:
        account = ACCOUNTS[i % 3]
        return account.auth_id, _dial(account)

    with ThreadPoolExecutor(workers) as threads:
        results = list(threads.map(dial, range(dials)))

    # End to end, every dial came back as the account it was placed on...
    for auth_id, call in results:
        assert call["status"] == "call_initiated"
        assert call["sid"].startswith(f"{auth_id}:")
    assert len(server.seen) == dials
    assert all(signed == in_path for _, signed, in_path in server.seen)
    # ...over at most one connection per thread, sockets carrying several accounts.
    assert server.connections <= workers
    accounts_per_socket: Dict[int, Set[str]] = {}
    for connection, signed, _ in server.seen:
        accounts_per_socket.setdefault(connection, set()).add(signed)
    assert any(len(accounts) > 1 for accounts in accounts_per_socket.values())


def test_a_connection_plivo_closed_is_replaced_not_reused(
    serve: Callable[..., _Plivo],
) -> None:
    server = serve(close_after_reply=True)
    for i in range(5):
        time.sleep(0.05)  # the close lands while the connection sits idle in the pool
        assert _dial(ACCOUNTS[i % 3])["status"] == ("call_initiated")
    assert server.connections == 5
    assert len(server.seen) == 5


def test_a_stalled_tls_handshake_frees_the_dial_thread_at_the_connect_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TCP connects (the kernel accepts) but TLS never answers: the thread is
    freed after the connect cap, not the read timeout, and the message carries
    the connect cap, so a stalled setup reads apart from a slow Plivo reply."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        port = listener.getsockname()[1]
        monkeypatch.setattr(
            sdk, "API_VOICE_BASE_URI", f"https://127.0.0.1:{port}/v1/Account"
        )
        monkeypatch.setattr(plivo_mod, "_PLIVO_TIMEOUT", (0.5, 5))
        provider = _dialler(ACCOUNTS[0])
        started = time.monotonic()
        with pytest.raises(requests.exceptions.ReadTimeout, match=r"read timeout=0\.5"):
            provider.client.calls.create(
                from_=NUMBER, to_=CUSTOMER, answer_url="https://bb.example.test/a"
            )
        assert time.monotonic() - started < 2.5
        assert provider.make_call(CUSTOMER, NUMBER) is None
    finally:
        listener.close()
