#!/bin/sh
# Start Ollama and ensure the configured model is present.
# Do not exit the container if pull fails when the model is already local
# (common when registry DNS is flaky offline).
set -e
ollama serve &
PID=$!

echo "Waiting for Ollama..."
i=0
while [ "$i" -lt 60 ]; do
  if ollama list >/dev/null 2>&1; then
    break
  fi
  i=$((i + 1))
  sleep 1
done

MODEL="${MODEL_NAME:-qwen2.5:3b}"  # from config.toml via compose
echo "Ensuring model ${MODEL} is available..."
if ollama show "${MODEL}" >/dev/null 2>&1; then
  echo "Model ${MODEL} already present — skip pull."
elif ! ollama pull "${MODEL}"; then
  echo "WARN: ollama pull ${MODEL} failed (network/DNS?)."
  if ollama show "${MODEL}" >/dev/null 2>&1; then
    echo "Model ${MODEL} is still listed locally — continuing."
  else
    echo "ERROR: model ${MODEL} is not available. Fix network and restart ollama."
    kill "$PID" 2>/dev/null || true
    exit 1
  fi
fi

wait "$PID"
