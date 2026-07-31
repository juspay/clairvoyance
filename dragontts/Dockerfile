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

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY app ./app

RUN uv sync --frozen

RUN useradd --uid 1000 --create-home appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--loop", "uvloop", "--no-access-log", "--limit-concurrency", "1024", "--timeout-graceful-shutdown", "100"]
