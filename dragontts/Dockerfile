FROM python:3.12-slim AS base

# Cap glibc malloc arenas so freed memory returns to the OS instead of fragmenting
# across dozens of pools. The default (8 x host-cores) lets the 4x32 worker
# threads strand freed audio buffers across ~100 arenas -> anon RSS creeps. 4 is a
# small fixed cap; zero perf cost on this 1-CPU, GIL-bound, async-I/O pod.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    MALLOC_ARENA_MAX=4

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

# ffmpeg powers the params.tempo atempo stage (eleven_v3_conversational only).
# Without it the tempo feature degrades to passthrough with an error log —
# calls never fail — but the image ships the binary so the feature works.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY app ./app

RUN uv sync --frozen

RUN useradd --uid 1000 --create-home appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# --timeout-keep-alive 8: keep idle client connections LONGER than clairvoyance's
# httpx client does (its keepalive_expiry default is 5 s). At uvicorn's default
# of 5 s both sides expire together, and a sentence sent ~5 s after the last one
# lands on a connection the server just closed -> "RemoteProtocolError: Server
# disconnected without sending a response" (that sentence is lost on the call).
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--loop", "uvloop", "--no-access-log", "--limit-concurrency", "1024", "--timeout-graceful-shutdown", "100", "--timeout-keep-alive", "8"]
