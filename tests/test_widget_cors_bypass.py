# pyrefly: ignore-errors
"""Tests for [[widget_cors_bypass.CustomWidgetCorsBypassMiddleware]].

Verifies the asymmetry the fix introduces:
  * widget public paths (``/agent/voice/breeze-buddy/widget/…``) are
    NOT subject to the static ``CORS_ALLOWED_ORIGINS`` list — any
    origin can preflight; per-merchant ``widget_config.allowed_origins``
    is the auth gate.
  * admin / RBAC paths still go through ``CORSMiddleware`` with the
    static list — unknown origins get rejected.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.middleware.widget_cors_bypass import CustomWidgetCorsBypassMiddleware


def _build_app() -> FastAPI:
    """Tiny FastAPI mirror with two routes — one widget-shaped, one
    admin-shaped — to exercise both middleware branches without pulling
    in the full breeze-buddy router stack."""
    app = FastAPI()

    @app.get("/agent/voice/breeze-buddy/widget/session")
    def widget_get() -> dict:
        return {"ok": True}

    # Mount our own preflight on the widget path that mirrors what
    # widget_common.options_cors_response() does in prod. Required
    # because the middleware bypasses the strict CORSMiddleware here
    # and lets the route handle preflight itself.
    @app.options("/agent/voice/breeze-buddy/widget/session")
    def widget_options() -> "Response":
        from starlette.responses import Response

        return Response(
            status_code=204,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers": "authorization, content-type",
                "Access-Control-Max-Age": "600",
            },
        )

    @app.get("/agent/voice/breeze-buddy/templates")
    def admin_get() -> dict:
        return {"ok": True}

    app.add_middleware(
        CustomWidgetCorsBypassMiddleware,
        allow_origins=["https://portal.breeze.in"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app


def test_widget_preflight_allowed_from_arbitrary_origin() -> None:
    """The whole point of the fix: a brand-new merchant origin must
    reach our handler so per-widget allowlist can run. Without the
    bypass the global allowlist would 400 here.

    Also asserts ACAO is exactly ``*`` (not ``*, *``) — the bypass
    injects ACAO when missing but must NOT duplicate the header the
    route-level OPTIONS handler already set. httpx joins duplicates
    with ``, ``, so an equality check on the single value catches it.
    """
    client = TestClient(_build_app())
    resp = client.options(
        "/agent/voice/breeze-buddy/widget/session",
        headers={
            "Origin": "https://firstbud.in",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert resp.status_code == 204
    assert resp.headers.get("access-control-allow-origin") == "*"


def test_widget_get_allowed_from_arbitrary_origin() -> None:
    """Confirms non-preflight requests on widget paths also bypass —
    AND that the response carries ``Access-Control-Allow-Origin: *``.

    Without ACAO on the response the browser would block JS from
    reading the body even though the request reached our handler.
    The bypass middleware has to inject it because we skip
    ``CORSMiddleware`` and the widget POST/GET handlers don't set
    CORS headers themselves (they return Pydantic models)."""
    client = TestClient(_build_app())
    resp = client.get(
        "/agent/voice/breeze-buddy/widget/session",
        headers={"Origin": "https://firstbud.in"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert resp.headers.get("access-control-allow-origin") == "*"
    assert "Origin" in resp.headers.get("vary", "")


def test_admin_preflight_rejected_from_unknown_origin() -> None:
    """Admin / RBAC paths must STILL be gated by the static list — the
    bypass is widget-only. portal.breeze.in is allowed; everything else
    should 400 on preflight."""
    client = TestClient(_build_app())
    resp = client.options(
        "/agent/voice/breeze-buddy/templates",
        headers={
            "Origin": "https://firstbud.in",
            "Access-Control-Request-Method": "GET",
        },
    )
    # Starlette's CORSMiddleware returns 400 for disallowed origins
    # on preflight, no allow-origin header echoed.
    assert resp.status_code == 400
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


def test_admin_preflight_allowed_from_listed_origin() -> None:
    """The static list still works for the origins it's meant to
    protect (portal/admin tools)."""
    client = TestClient(_build_app())
    resp = client.options(
        "/agent/voice/breeze-buddy/templates",
        headers={
            "Origin": "https://portal.breeze.in",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "https://portal.breeze.in"


def test_widget_config_admin_path_stays_strict() -> None:
    """Trailing-slash discipline: ``/widget-config`` (admin CRUD) must
    NOT be bypassed — only ``/widget/`` (note the slash) is the public
    surface. This guards against accidentally widening the bypass to
    the admin widget-config endpoints if someone trims the prefix."""
    app = FastAPI()

    @app.get("/agent/voice/breeze-buddy/widget-config")
    def widget_config() -> dict:
        return {"ok": True}

    app.add_middleware(
        CustomWidgetCorsBypassMiddleware,
        allow_origins=["https://portal.breeze.in"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    client = TestClient(app)
    resp = client.options(
        "/agent/voice/breeze-buddy/widget-config",
        headers={
            "Origin": "https://firstbud.in",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 400
