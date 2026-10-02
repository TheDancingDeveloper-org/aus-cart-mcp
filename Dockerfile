FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

RUN chmod -R a+rX /app && useradd --system --uid 10001 auscart && mkdir -p /data && chown auscart /data
USER auscart
ENV AUS_CART_MCP_DB=/data/aus-cart.db PORT=8080 PATH="/app/.venv/bin:$PATH"
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import httpx; httpx.get('http://127.0.0.1:8080/healthz').raise_for_status()"
CMD ["python", "-m", "aus_cart_mcp", "serve"]
