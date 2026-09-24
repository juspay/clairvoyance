"""Phase 1 of provider accounts (docs/PROVIDER_CREDENTIALS.md): the shapes a
credential row's value must have, and who may use a row."""

from typing import Any, Dict

from app.ai.voice.agents.breeze_buddy.accounts import SHAPES, in_tenant, shape_problems
from app.schemas import Credential, CredentialType

ROW = "0ec1c06d-b2e2-4b38-8c19-f9789b3482bf"


def _cred(**over: Any) -> Credential:
    base: Dict[str, Any] = dict(
        id=ROW,
        reseller_id="r-1",
        merchant_id=None,
        name="elevenlabs-prod",
        credential_type=CredentialType.CUSTOM,
        value={"api_key": "xi-secret"},
        is_encrypted=True,
        is_active=True,
        provider="elevenlabs",
    )
    base.update(over)
    return Credential(**base)


def test_a_value_must_have_its_vendors_shape() -> None:
    assert shape_problems("azure_openai", {"api_key": "k"}) == [
        "endpoint: Field required"
    ]
    assert shape_problems("azure_openai", {"api_key": "k", "endpoint": "x"}) == [
        "endpoint: Value error, endpoint 'x' is not an https:// or wss:// URL"
    ]
    assert shape_problems("google_vertex", {"credentials_json": "{}"}) == [
        "project_id: Field required"
    ]
    assert shape_problems("aws_bedrock", {}) == []  # the credential chain
    assert shape_problems("deepgram", {"api_key": " "}) == [
        "api_key: Value error, api_key is empty"
    ]
    assert shape_problems("nope", {"api_key": "k"})[0].startswith("unknown provider")
    # a value is exactly its vendor's fields: a host under another name is refused
    assert shape_problems("openai", {"api_key": "k", "base_url": "https://gw"}) == [
        "base_url: Extra inputs are not permitted"
    ]
    assert shape_problems("google", {"credentials_json": "{}", "endpoint": "x"}) == [
        "endpoint: Extra inputs are not permitted"
    ]
    # an endpoint is encrypted transport only, and Azure's is required
    assert shape_problems("openai", {"api_key": "k", "endpoint": "http://gw"}) == [
        "endpoint: Value error, endpoint 'http://gw' is not an https:// or wss:// URL"
    ]
    assert shape_problems("azure_openai", {"api_key": "k", "endpoint": ""}) == [
        "endpoint: Value error, endpoint is empty"
    ]
    # every vendor word validates its own shape; a complete value is clean
    assert shape_problems("elevenlabs", {"api_key": "k"}) == []
    assert shape_problems("google", {"credentials_json": "{}"}) == []
    assert set(SHAPES) >= {"azure_openai", "openai", "elevenlabs", "deepgram"}


def test_a_row_is_usable_by_its_own_tenant_and_global_rows_by_everyone() -> None:
    assert in_tenant(_cred(reseller_id=None), "r-9", "m-9")
    assert in_tenant(_cred(reseller_id="r-1", merchant_id=None), "r-1", "m-9")
    assert not in_tenant(_cred(reseller_id="r-1"), "r-2", "m-1")
    assert in_tenant(_cred(reseller_id="r-1", merchant_id="m-1"), "r-1", "m-1")
    assert not in_tenant(_cred(reseller_id="r-1", merchant_id="m-1"), "r-1", "m-2")
    assert not in_tenant(_cred(reseller_id="r-1"), None, None)
