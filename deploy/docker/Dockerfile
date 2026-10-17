# syntax=docker/dockerfile:1
# One image for every stage (design.md "Deployment"): the `microcast` CLI with all
# extras. Build from the repo root:
#   docker buildx build -f deploy/docker/Dockerfile .
# The lake, caches and MLflow db live under MICROCAST_DATA_DIR (/data, a volume);
# secrets and private places come from the environment, never the image.

FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

FROM python:3.12-slim-bookworm

# libgomp1: LightGBM's OpenMP runtime. eccodes ships in the eccodeslib wheel.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first, so source edits don't reinstall them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --all-extras --no-install-project

# Editable install keeps settings.REPO_ROOT at /app, so config/ and routes/ resolve.
COPY . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --all-extras

ARG GIT_SHA=unknown
# Herbie writes ~/.config/herbie on import, so the user needs a real home.
RUN useradd --create-home --uid 10001 microcast \
    && mkdir /data && chown microcast /data
USER microcast
# Write Herbie's default config now, so runs don't print its first-run banner.
RUN /app/.venv/bin/python -c "import herbie"

ENV PATH="/app/.venv/bin:$PATH" \
    MICROCAST_DATA_DIR=/data \
    MICROCAST_GIT_SHA=$GIT_SHA
VOLUME /data

ENTRYPOINT ["microcast"]
CMD ["--help"]
