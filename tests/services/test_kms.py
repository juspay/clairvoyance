"""decrypt_kms (app/services/kms.py) and its two provider branches.

    CLOUD_ENVIRONMENT=AWS                    -> boto3 decrypt, retry once, None on failure
    CLOUD_ENVIRONMENT=GCP + GCP_KMS_KEY_NAME -> Google KMS decrypt, retry once, None on failure
    CLOUD_ENVIRONMENT=GCP, key unset         -> passthrough (today's behaviour, untouched)
    any other provider                       -> passthrough
    dev / beta+SKIP_KMS_DECRYPT              -> passthrough before any provider is asked

No network: the provider clients are replaced by fakes at the module seam
(get_aws_client / get_gcp_kms_client).
"""

from __future__ import annotations

import base64
from typing import Any, Dict, List, Optional

import google_crc32c
import pytest

import app.services.aws.kms as aws_kms
import app.services.gcp.kms as gcp_kms
import app.services.kms as kms_dispatch

KEY_NAME = "projects/p/locations/asia-south1/keyRings/r/cryptoKeys/k"
PLAINTEXT = "pg-pass"
RAW_CIPHERTEXT = b"\x00\x01opaque-kms-bytes"
CIPHERTEXT_B64 = base64.b64encode(RAW_CIPHERTEXT).decode()


# --- doubles --------------------------------------------------------------------


class _Plaintext:
    def __init__(self, plaintext: bytes, crc_offset: int = 0) -> None:
        self.plaintext = plaintext
        # The CRC Google would compute; crc_offset simulates a corrupted reply.
        self.plaintext_crc32c = int(google_crc32c.value(plaintext)) + crc_offset


class FakeGcpClient:
    """KeyManagementServiceAsyncClient stand-in: records decrypt calls,
    optionally raising, and honours the async context-manager protocol."""

    def __init__(
        self,
        plaintexts: Optional[List[Optional[bytes]]] = None,
        raise_on: Optional[List[bool]] = None,
        crc_offset: int = 0,
    ) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.plaintexts = plaintexts or [PLAINTEXT.encode()]
        self.raise_on = raise_on or []
        self.crc_offset = crc_offset
        self.closed = False

    async def __aenter__(self) -> "FakeGcpClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def decrypt(self, *, request: Dict[str, Any]) -> _Plaintext:
        index = len(self.calls)
        self.calls.append(dict(request))
        if index < len(self.raise_on) and self.raise_on[index]:
            raise RuntimeError("google says no")
        plaintext = self.plaintexts[min(index, len(self.plaintexts) - 1)]
        return _Plaintext(
            plaintext if plaintext is not None else b"", crc_offset=self.crc_offset
        )


class FakeAwsClient:
    def __init__(self, responses: List[Dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: List[Dict[str, Any]] = []

    def decrypt(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def _env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cloud: str,
    environment: str = "production",
    skip: bool = False,
    gcp_key: str = "",
) -> None:
    monkeypatch.setattr(kms_dispatch, "CLOUD_ENVIRONMENT", cloud)
    monkeypatch.setattr(kms_dispatch, "ENVIRONMENT", environment)
    monkeypatch.setattr(kms_dispatch, "SKIP_KMS_DECRYPT", skip)
    monkeypatch.setattr(kms_dispatch, "GCP_KMS_KEY_NAME", gcp_key)
    monkeypatch.setattr(gcp_kms, "GCP_KMS_KEY_NAME", gcp_key)


def _gcp_client(monkeypatch: pytest.MonkeyPatch, client: Optional[FakeGcpClient]):
    factory_calls: List[int] = []

    def factory() -> Optional[FakeGcpClient]:
        factory_calls.append(1)
        return client

    monkeypatch.setattr(gcp_kms, "get_gcp_kms_client", factory)
    return factory_calls


def _aws_client(monkeypatch: pytest.MonkeyPatch, client: Optional[FakeAwsClient]):
    factory_calls: List[str] = []

    def factory(service_name: str) -> Optional[FakeAwsClient]:
        factory_calls.append(service_name)
        return client

    monkeypatch.setattr(aws_kms, "get_aws_client", factory)
    return factory_calls


# --- GCP ------------------------------------------------------------------------


async def test_gcp_with_key_decrypts_base64_ciphertext(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, cloud="GCP", gcp_key=KEY_NAME)
    client = FakeGcpClient()
    _gcp_client(monkeypatch, client)

    assert await kms_dispatch.decrypt_kms(CIPHERTEXT_B64) == PLAINTEXT
    assert client.calls == [
        {
            "name": KEY_NAME,
            "ciphertext": RAW_CIPHERTEXT,
            "ciphertext_crc32c": int(google_crc32c.value(RAW_CIPHERTEXT)),
        }
    ]
    assert client.closed is True


async def test_gcp_response_crc_mismatch_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A plaintext whose CRC32C does not match what Google reports is never
    # handed back as a password, even though decrypt() itself "succeeded".
    _env(monkeypatch, cloud="GCP", gcp_key=KEY_NAME)
    client = FakeGcpClient(crc_offset=1)
    _gcp_client(monkeypatch, client)

    assert await kms_dispatch.decrypt_kms(CIPHERTEXT_B64) is None
    assert len(client.calls) == 2


async def test_gcp_without_key_is_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, cloud="GCP", gcp_key="")
    factory_calls = _gcp_client(monkeypatch, FakeGcpClient())

    assert await kms_dispatch.decrypt_kms("plain-password") == "plain-password"
    assert factory_calls == []


async def test_gcp_google_error_returns_none_after_one_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, cloud="GCP", gcp_key=KEY_NAME)
    client = FakeGcpClient(raise_on=[True, True])
    _gcp_client(monkeypatch, client)

    assert await kms_dispatch.decrypt_kms(CIPHERTEXT_B64) is None
    assert len(client.calls) == 2
    assert client.closed is True


async def test_gcp_retry_succeeds_on_second_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, cloud="GCP", gcp_key=KEY_NAME)
    client = FakeGcpClient(raise_on=[True, False])
    _gcp_client(monkeypatch, client)

    assert await kms_dispatch.decrypt_kms(CIPHERTEXT_B64) == PLAINTEXT
    assert len(client.calls) == 2


async def test_gcp_empty_plaintext_is_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, cloud="GCP", gcp_key=KEY_NAME)
    client = FakeGcpClient(plaintexts=[None])
    _gcp_client(monkeypatch, client)

    assert await kms_dispatch.decrypt_kms(CIPHERTEXT_B64) is None
    assert len(client.calls) == 2


async def test_gcp_invalid_base64_returns_none_without_calling_google(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, cloud="GCP", gcp_key=KEY_NAME)
    client = FakeGcpClient()
    _gcp_client(monkeypatch, client)

    assert await kms_dispatch.decrypt_kms("not base64!!") is None
    assert client.calls == []


async def test_gcp_client_unavailable_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, cloud="GCP", gcp_key=KEY_NAME)
    _gcp_client(monkeypatch, None)

    assert await kms_dispatch.decrypt_kms(CIPHERTEXT_B64) is None


async def test_gcp_direct_call_without_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The dispatcher never routes here without a key; the module still
    # refuses on its own rather than sending a request with name="".
    monkeypatch.setattr(gcp_kms, "GCP_KMS_KEY_NAME", "")
    factory_calls = _gcp_client(monkeypatch, FakeGcpClient())

    assert await gcp_kms.decrypt_with_gcp_kms(CIPHERTEXT_B64) is None
    assert factory_calls == []


# --- GCP client construction ----------------------------------------------------


def test_gcp_client_uses_service_account_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sa_info = {"type": "service_account", "project_id": "proj-1"}
    seen: Dict[str, Any] = {}

    def from_info(info: Dict[str, Any]) -> str:
        seen["info"] = info
        return "sa-credentials"

    class _Client:
        def __init__(self, credentials: Optional[str] = None) -> None:
            seen["credentials"] = credentials

    monkeypatch.setattr(
        gcp_kms,
        "GCP_KMS_CREDENTIALS_JSON",
        '{"type": "service_account", "project_id": "proj-1"}',
    )
    monkeypatch.setattr(
        gcp_kms.service_account.Credentials, "from_service_account_info", from_info
    )
    monkeypatch.setattr(gcp_kms.kms, "KeyManagementServiceAsyncClient", _Client)

    assert isinstance(gcp_kms.get_gcp_kms_client(), _Client)
    assert seen == {"info": sa_info, "credentials": "sa-credentials"}


def test_gcp_client_falls_back_to_default_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: Dict[str, Any] = {}

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            seen["kwargs"] = kwargs

    monkeypatch.setattr(gcp_kms, "GCP_KMS_CREDENTIALS_JSON", "")
    monkeypatch.setattr(gcp_kms.kms, "KeyManagementServiceAsyncClient", _Client)

    assert isinstance(gcp_kms.get_gcp_kms_client(), _Client)
    assert seen == {"kwargs": {}}


def test_gcp_client_malformed_json_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gcp_kms, "GCP_KMS_CREDENTIALS_JSON", "{not json")

    assert gcp_kms.get_gcp_kms_client() is None


def test_gcp_client_constructor_error_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**kwargs: Any) -> None:
        raise RuntimeError("no ADC on this box")

    monkeypatch.setattr(gcp_kms, "GCP_KMS_CREDENTIALS_JSON", "")
    monkeypatch.setattr(gcp_kms.kms, "KeyManagementServiceAsyncClient", boom)

    assert gcp_kms.get_gcp_kms_client() is None


# --- AWS (unchanged behaviour) --------------------------------------------------


async def test_aws_decrypts_with_boto_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, cloud="AWS", gcp_key=KEY_NAME)
    client = FakeAwsClient([{"Plaintext": PLAINTEXT.encode()}])
    aws_calls = _aws_client(monkeypatch, client)
    gcp_calls = _gcp_client(monkeypatch, FakeGcpClient())

    assert await kms_dispatch.decrypt_kms(CIPHERTEXT_B64) == PLAINTEXT
    assert aws_calls == ["kms"]
    assert client.calls == [{"CiphertextBlob": RAW_CIPHERTEXT}]
    assert gcp_calls == []


async def test_aws_retries_once_then_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, cloud="AWS")
    client = FakeAwsClient([{}])
    _aws_client(monkeypatch, client)

    assert await kms_dispatch.decrypt_kms(CIPHERTEXT_B64) is None
    assert len(client.calls) == 2


async def test_aws_client_unavailable_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, cloud="AWS")
    _aws_client(monkeypatch, None)

    assert await kms_dispatch.decrypt_kms(CIPHERTEXT_B64) is None


# --- passthroughs ---------------------------------------------------------------


async def test_unknown_provider_is_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, cloud="AZURE", gcp_key=KEY_NAME)
    aws_calls = _aws_client(monkeypatch, None)
    gcp_calls = _gcp_client(monkeypatch, None)

    assert await kms_dispatch.decrypt_kms("plain-password") == "plain-password"
    assert aws_calls == []
    assert gcp_calls == []


@pytest.mark.parametrize("cloud", ["AWS", "GCP"])
async def test_dev_environment_skips_every_provider(
    monkeypatch: pytest.MonkeyPatch, cloud: str
) -> None:
    _env(monkeypatch, cloud=cloud, environment="dev", gcp_key=KEY_NAME)
    aws_calls = _aws_client(monkeypatch, None)
    gcp_calls = _gcp_client(monkeypatch, None)

    assert await kms_dispatch.decrypt_kms("plain-password") == "plain-password"
    assert aws_calls == []
    assert gcp_calls == []


@pytest.mark.parametrize("cloud", ["AWS", "GCP"])
async def test_beta_with_skip_flag_skips_every_provider(
    monkeypatch: pytest.MonkeyPatch, cloud: str
) -> None:
    _env(monkeypatch, cloud=cloud, environment="beta", skip=True, gcp_key=KEY_NAME)
    aws_calls = _aws_client(monkeypatch, None)
    gcp_calls = _gcp_client(monkeypatch, None)

    assert await kms_dispatch.decrypt_kms("plain-password") == "plain-password"
    assert aws_calls == []
    assert gcp_calls == []


async def test_beta_without_skip_flag_still_decrypts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, cloud="GCP", environment="beta", skip=False, gcp_key=KEY_NAME)
    client = FakeGcpClient()
    _gcp_client(monkeypatch, client)

    assert await kms_dispatch.decrypt_kms(CIPHERTEXT_B64) == PLAINTEXT
    assert len(client.calls) == 1


async def test_empty_input_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, cloud="GCP", gcp_key=KEY_NAME)
    factory_calls = _gcp_client(monkeypatch, FakeGcpClient())

    assert await kms_dispatch.decrypt_kms("") is None
    assert factory_calls == []
