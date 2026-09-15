FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir -e .

RUN mkdir -p /workspace

ENV WORKSPACE_DIR=/workspace \
    OLLAMA_BASE_URL=http://ollama:11434 \
    MODEL_NAME=qwen2.5:3b \
    NUM_CTX=2048 \
    THINKING_ENABLED=true \
    PYTHONUNBUFFERED=1

COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /workspace
ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
