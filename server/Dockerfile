FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

WORKDIR /srv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/srv/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY alembic.ini ./
COPY app ./app
COPY migrations ./migrations

RUN useradd --system --uid 10001 ref && mkdir -p /var/lib/ref/frames && chown -R ref /var/lib/ref
USER ref

EXPOSE 8000
# Apply migrations, then serve. Fails loudly if the database is unreachable.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
