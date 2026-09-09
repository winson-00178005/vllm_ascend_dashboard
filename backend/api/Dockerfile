# Production Dockerfile for vLLM Ascend Dashboard Backend
# Multi-stage build for optimized image size

# Stage 1: Install dependencies
# 国内网络环境使用镜像加速
FROM ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa AS uv

FROM docker.m.daocloud.io/library/python:3.11-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff AS dependencies

WORKDIR /app

# Install uv (ghcr.io 通常可从国内访问，若不行可换为 pip install uv)
COPY --from=uv /uv /usr/local/bin/uv

# Copy pyproject.toml and README.md (required by hatchling)
COPY backend/pyproject.toml backend/uv.lock backend/README.md ./

# Create virtual environment and install dependencies from pyproject.toml
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN uv sync --frozen --no-dev --no-install-project


# Stage 2: Production
FROM docker.m.daocloud.io/library/python:3.11-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff AS production

WORKDIR /app

# Configure apt mirrors for Chinese network (Aliyun)
RUN sed -i 's|http://deb.debian.org/debian|http://mirrors.aliyun.com/debian|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true

# Install runtime dependencies (with retry for flaky network)
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    for i in 1 2 3; do \
        apt-get update && apt-get install -y --no-install-recommends \
            curl ca-certificates git procps cloc \
            libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libffi-dev shared-mime-info fonts-noto-cjk \
            && break || sleep 10; \
    done \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash appuser

# Install Node.js 20.x LTS + Claude Code CLI
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    for i in 1 2 3; do \
        curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
        && apt-get install -y nodejs \
        && break || sleep 10; \
    done \
    && npm config set registry https://registry.npmmirror.com \
    && for i in 1 2 3; do \
        npm install -g @anthropic-ai/claude-code jscpd && break || sleep 10; \
    done \
    && npm config delete registry \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment
COPY --from=dependencies /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install lizard for code complexity analysis
# Copy application code
COPY backend/api/ ./api/
COPY backend/scheduler/ ./scheduler/
COPY backend/collector/ ./collector/
COPY backend/infrastructure/ ./infrastructure/
COPY backend/contracts/ ./contracts/
COPY backend/agent/ ./agent/
COPY backend/reporting/ ./reporting/
COPY backend/project_dashboard/ ./project_dashboard/
COPY backend/resource_dashboard/ ./resource_dashboard/
COPY backend/log_center/ ./log_center/
COPY backend/tooling/ ./tooling/
COPY backend/failure_analysis/ ./failure_analysis/
COPY backend/test_board/ ./test_board/
COPY backend/model_sync/ ./model_sync/
COPY backend/support_matrix/ ./support_matrix/
COPY backend/pr_pipeline/ ./pr_pipeline/
COPY backend/nightly_gantt/ ./nightly_gantt/
COPY backend/resources/ ./resources/
# The JSON file is an initial seed only. Runtime updates are persisted in the
# model_fo_mappings table, so keep the seed outside the mounted /app/data volume.
COPY data/model_fo_map.json ./resources/model_fo_map.json
COPY backend/templates/ ./templates/

# Copy skills/agents
COPY backend/.agents/ ./.agents/

# Install skills for Claude Code CLI (so CLI finds them natively)
RUN mkdir -p /home/appuser/.claude/skills \
    && cp -r ./.agents/skills/* /home/appuser/.claude/skills/ \
    && chown -R appuser:appuser /home/appuser/.claude

# Keep database bootstrap and migrations under their source-aligned package.
COPY database/ ./database/

# Copy entrypoint script
COPY backend/api/docker-entrypoint.sh /docker-entrypoint.sh
RUN sed -i 's/\r$//' /docker-entrypoint.sh && chmod +x /docker-entrypoint.sh

# Create data and logs directories for persistent storage
# Note: Permissions will be fixed by entrypoint script after volume mount
RUN mkdir -p /app/data /app/logs

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ENVIRONMENT=production \
    DEBUG=false \
    HOME=/home/appuser

# Don't switch to non-root user here - entrypoint script handles it
# USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Use entrypoint script to fix permissions and start application
# Note: ENTRYPOINT runs as root to fix volume permissions, then switches to appuser
ENTRYPOINT ["/docker-entrypoint.sh"]
