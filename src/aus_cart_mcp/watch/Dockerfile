FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

RUN chmod -R a+rX /app && useradd --system --uid 10001 cartwatch && mkdir -p /data && chown cartwatch /data
USER cartwatch
ENV AUS_CARTWATCH_DB=/data/aus-cartwatch.db PORT=8080 PATH="/app/.venv/bin:$PATH"
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os, httpx; httpx.get(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8080\")}/healthz').raise_for_status()"
CMD ["python", "-m", "aus_cartwatch", "serve"]
