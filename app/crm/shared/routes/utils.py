"""The helpers the route classes are built from, callable on their own by a
route that cannot take the class — record's webhook bays read through
read_bounded themselves, because an unknown bay is refused before a single
body byte is read."""

from typing import Optional

from fastapi import HTTPException, Request, status
from starlette.requests import ClientDisconnect


async def read_bounded(request: Request, max_bytes: int) -> Optional[bytes]:
    """The request body, or None once it passes ``max_bytes``.

    A declared Content-Length over the cap is refused before a byte is read;
    otherwise the read itself stops at the cap, so a chunked or understated
    body cannot buffer more than the cap allows. A client that hangs up
    mid-body gets FastAPI's own answer to a body it could not read, a 400,
    rather than a 500 and an ERROR traceback for a caller who simply left."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        return None
    body = bytearray()
    try:
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > max_bytes:
                return None
    except ClientDisconnect:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="There was an error parsing the body",
        )
    return bytes(body)
