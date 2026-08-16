"""The version snapshot must never contain real MCP auth material.

Locks the mechanism the handlers rely on: dumping ConfigurationModel
WITHOUT reveal_secrets masks HttpAuthConfig secret fields, while the
revealed dump (used for the live template row) does not.
"""

from app.ai.voice.agents.breeze_buddy.template.types import ConfigurationModel


def _config_with_mcp_auth() -> ConfigurationModel:
    return ConfigurationModel(
        mcp={
            "servers": [
                {
                    "name": "crm",
                    "url": "https://mcp.example.com",
                    "auth": {"type": "bearer", "token": "real-secret-token"},
                }
            ]
        }
    )


def test_unrevealed_dump_masks_auth_token():
    snapshot = _config_with_mcp_auth().model_dump(exclude_none=True, mode="json")
    token = snapshot["mcp"]["servers"][0]["auth"]["token"]
    assert token == "**********"
    assert "real-secret-token" not in str(snapshot)


def test_revealed_dump_keeps_real_token():
    revealed = _config_with_mcp_auth().model_dump(
        exclude_none=True, mode="json", context={"reveal_secrets": True}
    )
    assert revealed["mcp"]["servers"][0]["auth"]["token"] == "real-secret-token"
