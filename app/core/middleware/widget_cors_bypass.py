"""Path-aware CORS dispatcher for the widget public surface.

Background — two CORS layers exist on this service:

1. **App-level FastAPI ``CORSMiddleware``** (``app/main.py``) — enforces
   the static ``CORS_ALLOWED_ORIGINS`` list. Right gate for admin /
   RBAC routes (portal-only surfaces) where the origin set is small
   and stable.

2. **Per-widget ``allowed_origins``** (``widget_common.py``
   :func:`resolve_widget_config_for_request`) — enforces the
   merchant-configured origin allowlist stored on the ``widget_config``
   row. The real production gate for the public widget surface;
   onboarding a new merchant means updating one DB row, not a redeploy.

The widget routes already mount their own ``@router.options(...)``
handlers that emit permissive preflight headers via
:func:`options_cors_response` so the merchant-side preflight always
succeeds and reaches our application logic. But the global
``CORSMiddleware`` runs first and 400s any preflight whose ``Origin``
isn't in the static list — so the route-level OPTIONS handler is
never reached, and adding a new merchant to production currently
requires editing the ``CORS_ALLOWED_ORIGINS`` env var + a rolling
restart.

This middleware fixes that asymmetry: requests against the public
widget path prefix bypass the static-list middleware entirely (and
hit the route-level OPTIONS / per-widget allowlist instead), while
every other path stays behind the strict static list.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Headers we inject on bypassed non-preflight responses when the handler
# hasn't already set them. ACAO=* is required for the browser to expose
# the response body to JS; without it, the request reaches our handler
# but the browser refuses to surface the result. Credentials stay off
# because ``Allow-Credentials: true`` + ``Origin: *`` is invalid per
# spec — widget tokens travel in ``Authorization``, so cookies aren't
# needed anyway. ``Vary: Origin`` keeps caches honest.
_BYPASS_RESPONSE_ACAO: tuple[bytes, bytes] = (
    b"access-control-allow-origin",
    b"*",
)
_BYPASS_RESPONSE_VARY: tuple[bytes, bytes] = (b"vary", b"Origin")

# Paths that bypass the global allowlist. Trailing slash is meaningful:
# ``/widget/`` matches the public session/message routes but not the
# admin-only ``/widget-config`` CRUD which must stay locked to portal
# origins.
_DEFAULT_BYPASS_PREFIXES: tuple[str, ...] = ("/agent/voice/breeze-buddy/widget/",)


class CustomWidgetCorsBypassMiddleware:
    """ASGI dispatcher: send widget paths straight through, everything
    else through ``CORSMiddleware``.

    The widget surface enforces origin allowlist per-merchant inside
    the route handlers (see ``widget_common.resolve_widget_config_for_request``)
    and emits its own preflight via ``@router.options(...)``. The
    static allowlist would only get in the way and force a redeploy
    for every new merchant onboarding, so widget paths bypass it.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        allow_origins: Sequence[str],
        allow_credentials: bool = True,
        allow_methods: Iterable[str] = ("*",),
        allow_headers: Iterable[str] = ("*",),
        bypass_path_prefixes: Sequence[str] = _DEFAULT_BYPASS_PREFIXES,
    ) -> None:
        self._inner_app = app
        # Wrap the inner app in the strict CORS middleware. Non-bypass
        # paths get the same behaviour as before this middleware was
        # introduced — drop-in compatible.
        self._cors_app = CORSMiddleware(
            app,
            allow_origins=list(allow_origins),
            allow_credentials=allow_credentials,
            allow_methods=list(allow_methods),
            allow_headers=list(allow_headers),
        )
        self._bypass_prefixes = tuple(bypass_path_prefixes)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Non-HTTP traffic (websockets, lifespan) doesn't touch CORS;
        # pass it through the same wrapped chain the strict middleware
        # uses so we don't accidentally drop lifespan events.
        if scope.get("type") != "http":
            await self._cors_app(scope, receive, send)
            return

        path = scope.get("path", "")
        if any(path.startswith(p) for p in self._bypass_prefixes):
            # Skip the static allowlist entirely. The widget routes
            # carry their own permissive OPTIONS handlers AND validate
            # the caller's origin against widget_config.allowed_origins
            # inside the POST handler — that's the auth gate.
            #
            # Inject ACAO on the way out so the browser exposes the
            # response body to JS. The OPTIONS handlers already set
            # their own ACAO via options_cors_response(); we only fill
            # it in when missing, so preflight responses pass through
            # untouched.
            await self._inner_app(scope, receive, self._wrap_send(send))
            return

        await self._cors_app(scope, receive, send)

    @staticmethod
    def _wrap_send(send: Send) -> Send:
        async def send_with_cors(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                lower_keys = {k.lower() for k, _ in headers}
                if b"access-control-allow-origin" not in lower_keys:
                    headers.append(_BYPASS_RESPONSE_ACAO)
                    if b"vary" not in lower_keys:
                        headers.append(_BYPASS_RESPONSE_VARY)
                message["headers"] = headers
            await send(message)

        return send_with_cors


__all__ = ["CustomWidgetCorsBypassMiddleware"]
