"""The route classes any CRM api.py hangs on its router (shared/routes/):
each is a base a module subclasses with its own policy, and a subclass that
forgets the policy must fail when its route is declared, not when its first
request arrives."""

from typing import Dict

import pytest
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.crm.shared.routes.classes import BoundedBodyRoute, TranslatingRoute


class _Declined(Exception):
    pass


def _client(router: APIRouter) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


def test_a_translating_route_without_translate_fails_at_declaration() -> None:
    class Forgot(TranslatingRoute):
        pass

    router = APIRouter(route_class=Forgot)
    with pytest.raises(TypeError, match="must define translate"):

        @router.get("/x")
        async def x() -> Dict[str, str]:
            return {}


def test_a_bounded_route_without_a_cap_fails_at_declaration() -> None:
    class Forgot(BoundedBodyRoute):
        pass

    router = APIRouter(route_class=Forgot)
    with pytest.raises(TypeError, match="must define max_body_bytes"):

        @router.post("/x")
        async def x() -> Dict[str, str]:
            return {}


def test_the_modules_errors_are_translated_and_the_rest_pass_through() -> None:
    class Mapped(TranslatingRoute):
        def translate(self, error: Exception):
            if isinstance(error, _Declined):
                return HTTPException(status_code=409, detail=str(error))
            return None

    router = APIRouter(route_class=Mapped)

    @router.get("/declined")
    async def declined() -> Dict[str, str]:
        raise _Declined("the provider declined")

    @router.get("/broken")
    async def broken() -> Dict[str, str]:
        raise RuntimeError("not the module's")

    client = _client(router)
    answer = client.get("/declined")
    assert answer.status_code == 409
    assert answer.json() == {"detail": "the provider declined"}
    # Not the module's: re-raised as it is, so FastAPI answers as it would.
    assert client.get("/broken").status_code == 500


def test_the_cap_is_asked_on_every_request() -> None:
    # Asked per request, not frozen when the route is declared: a module keeps
    # its cap in one constant that its other readers and its tests can move.
    cap = {"bytes": 8}

    class Capped(BoundedBodyRoute):
        def max_body_bytes(self) -> int:
            return cap["bytes"]

    router = APIRouter(route_class=Capped)

    @router.post("/in")
    async def take(request: Request) -> Dict[str, int]:
        return {"read": len(await request.body())}

    client = _client(router)
    assert client.post("/in", content=b"x" * 8).json() == {"read": 8}
    refused = client.post("/in", content=b"x" * 9)
    assert refused.status_code == 413
    assert refused.json() == {"detail": "Request body exceeds 8 bytes"}

    cap["bytes"] = 16
    assert client.post("/in", content=b"x" * 9).json() == {"read": 9}
