"""Route classes any CRM ``api.py`` can hang on its router:
``APIRouter(route_class=...)``.

A route class wraps every route on its router, so what it guarantees is
guaranteed for the next route added beside them without that route declaring
anything — which is why these are route classes and not dependencies or
per-route code. Each is a base the module SUBCLASSES with its own policy
(its exception table, its body cap); a subclass that forgets the policy fails
when its first route is declared, at import, not on its first request.

- ``TranslatingRoute`` — the module's exception families become status codes
  in one place, so a route never writes try/except and two routes cannot map
  one family to two codes.
- ``BoundedBodyRoute`` — the body is read up to the module's cap BEFORE
  FastAPI parses it or runs a dependency (auth included).
"""

from typing import Any, Callable, Coroutine, Optional

from fastapi import HTTPException, Request, Response, status
from fastapi.routing import APIRoute

from app.crm.shared.routes.utils import read_bounded


class TranslatingRoute(APIRoute):
    """The one place a module's exceptions become status codes.

    Subclass it with ``translate``: PURE, an exception -> the HTTPException
    it earns, or None for anything that is not the module's (re-raised as it
    is, so FastAPI keeps its own answers)."""

    def translate(self, error: Exception) -> Optional[HTTPException]:
        raise NotImplementedError

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        if type(self).translate is TranslatingRoute.translate:
            raise TypeError(f"{type(self).__name__} must define translate()")
        handler = super().get_route_handler()

        async def _translated(request: Request) -> Response:
            try:
                return await handler(request)
            except Exception as error:  # noqa: BLE001 — re-raised unless the module's
                translated = self.translate(error)
                if translated is None:
                    raise
                raise translated from error

        return _translated


class BoundedBodyRoute(APIRoute):
    """Every route on the router reads its body through read_bounded before
    FastAPI's own handler runs.

    A size check declared as a dependency comes too late: FastAPI reads and
    parses a body parameter whole before any dependency runs, auth included,
    so an unauthenticated caller could make it buffer any size. The cap sits
    on the route class, ahead of that read, and FastAPI then parses the bytes
    already read: its validation, its 422s and its OpenAPI stay its own.

    Subclass it with ``max_body_bytes``. It is asked on every request, so a
    module keeps its cap in ONE constant that its other readers (and its
    tests) share."""

    def max_body_bytes(self) -> int:
        raise NotImplementedError

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        if type(self).max_body_bytes is BoundedBodyRoute.max_body_bytes:
            raise TypeError(f"{type(self).__name__} must define max_body_bytes()")
        handler = super().get_route_handler()

        async def bounded_handler(request: Request) -> Response:
            limit = self.max_body_bytes()
            body = await read_bounded(request, limit)
            if body is None:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Request body exceeds {limit} bytes",
                )
            # Starlette's request.body() answers from _body once it is set, so
            # FastAPI's read inside the handler gets these bytes, nothing more.
            request._body = body
            return await handler(request)

        return bounded_handler
