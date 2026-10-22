# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.13-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.9.26@sha256:9a23023be68b2ed09750ae636228e903a54a05ea56ed03a934d00fe9fbeded4b

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.13
WORKDIR /src

COPY docker/upstream-requirements.txt /tmp/upstream-requirements.txt
RUN uv venv /opt/upstream \
    && uv pip install --python /opt/upstream/bin/python --require-hashes --no-deps \
        -r /tmp/upstream-requirements.txt \
    && /opt/upstream/bin/mcp-atlassian --help > /dev/null

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN UV_PROJECT_ENVIRONMENT=/opt/wrapper uv sync --frozen --no-dev --no-editable \
    && /opt/wrapper/bin/mcp-atlassian-better --version

FROM ${PYTHON_IMAGE}
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/app --shell /usr/sbin/nologin app
COPY --from=build /opt/upstream /opt/upstream
COPY --from=build /opt/wrapper /opt/wrapper
ENV PATH=/opt/wrapper/bin:$PATH \
    HOME=/home/app \
    XDG_STATE_HOME=/tmp/state \
    PYTHONDONTWRITEBYTECODE=1 \
    MCP_ATLASSIAN_BETTER_CONFIG=/etc/mcp-atlassian-better/config.toml \
    MCP_ATLASSIAN_BETTER_UPSTREAM_COMMAND=/opt/upstream/bin/mcp-atlassian \
    MCP_ATLASSIAN_BETTER_FORCE_READ_ONLY=true \
    MCP_ATLASSIAN_BETTER_TRANSPORT=streamable-http \
    MCP_ATLASSIAN_BETTER_HOST=0.0.0.0 \
    MCP_ATLASSIAN_BETTER_PORT=8000
WORKDIR /home/app
USER 1000:1000
EXPOSE 8000
ENTRYPOINT ["mcp-atlassian-better"]
