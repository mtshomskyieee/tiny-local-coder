FROM python:3.12-slim

WORKDIR /app

# Toolchains the agent installed at runtime get recorded in
# workspace/.toolchains and passed back in here by build-service.sh, so a
# rebuild bakes them in instead of re-installing them every session.
ARG EXTRA_APT_PACKAGES=""

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ${EXTRA_APT_PACKAGES} \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml README.md config.toml ./
COPY src ./src

RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir -e .

RUN mkdir -p /workspace

ENV WORKSPACE_DIR=/workspace \
    OLLAMA_BASE_URL=http://ollama:11434 \
    TLC_CONFIG=/app/config.toml \
    MODEL_NAME=qwen2.5:3b \
    NUM_CTX=2048 \
    THINKING_ENABLED=true \
    AUTO_INSTALL=true \
    PYTHONUNBUFFERED=1

COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /workspace
ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
