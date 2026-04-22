FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Install dependencies (without project source) for better layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Install the project itself
COPY src/ ./src/
COPY README.md ./
RUN uv sync --frozen --no-dev


FROM python:3.14-slim-bookworm

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH"

# Image metadata
LABEL org.opencontainers.image.title="j2i" \
      org.opencontainers.image.description="Open source bridge software that connects XMPP and IRC channels" \
      org.opencontainers.image.authors="Telepath" \
      org.opencontainers.image.url="https://telepath.im/projects/j2i/" \
      org.opencontainers.image.source="https://foundry.fsky.io/telepath/j2i" \
      org.opencontainers.image.licenses="Unlicense"

ENTRYPOINT ["j2i"]
CMD ["-c", "/config/config.toml"]
