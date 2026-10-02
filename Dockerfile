FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable --extra biometrics

FROM python:3.12-slim
RUN groupadd --gid 10001 muth && useradd --uid 10001 --gid 10001 --create-home muth
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
RUN mkdir /app/data && chown muth:muth /app/data
USER muth
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["uvicorn", "muth.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
