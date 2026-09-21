# Source after ROOT, MODEL_NAME and NUM_CTX are set.
# tlc_probe_model  tlc_resident_report
#
# Answers "does this context size actually work here" with measurements rather
# than estimates: Ollama reports load/prompt-eval/eval durations per request,
# and /api/ps reports what the loaded model is really costing.

TLC_OLLAMA_URL="${OLLAMA_BASE_URL:-http://127.0.0.1:11434}"

# Resident size of the loaded model, straight from Ollama.
tlc_resident_report() {
  local json
  json="$(curl -fsS --max-time 10 "$TLC_OLLAMA_URL/api/ps" 2>/dev/null || true)"
  [[ -z "$json" ]] && return 0
  printf '%s' "$json" | python3 -c '
import json, sys
try:
    models = json.load(sys.stdin).get("models") or []
except Exception:
    sys.exit(0)
for m in models:
    size = (m.get("size") or 0) / 1024**3
    name = m.get("name") or "?"
    ctx = m.get("context_length") or m.get("num_ctx")
    line = "  resident       %5.2f GB   %s" % (size, name)
    if ctx:
        line += " at ctx %s" % ctx
    print(line)
' 2>/dev/null || true
}

# One short generation at the configured context. Prints load time, time to
# first token and throughput, then a verdict.
tlc_probe_model() {
  local model="${MODEL_NAME:-}" ctx="${NUM_CTX:-2048}" payload json
  [[ -z "$model" ]] && return 0
  if ! command -v curl >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
    return 0
  fi

  echo "Probing $model at ctx $ctx …"
  payload="$(python3 -c '
import json, sys
print(json.dumps({
    "model": sys.argv[1],
    "prompt": "Reply with exactly one short sentence about the C language.",
    "stream": False,
    "options": {"num_ctx": int(sys.argv[2]), "num_predict": 32, "temperature": 0},
}))' "$model" "$ctx")"

  json="$(curl -fsS --max-time 300 "$TLC_OLLAMA_URL/api/generate" \
    -H 'Content-Type: application/json' -d "$payload" 2>/dev/null || true)"

  if [[ -z "$json" ]]; then
    echo "  probe did not complete — Ollama did not answer in time" >&2
    return 0
  fi

  printf '%s' "$json" | python3 -c '
import json, sys

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)

ns = 1_000_000_000
load = (d.get("load_duration") or 0) / ns
pe = (d.get("prompt_eval_duration") or 0) / ns
ev = (d.get("eval_duration") or 0) / ns
n = d.get("eval_count") or 0
total = (d.get("total_duration") or 0) / ns

# Time to first token: the model has to be resident and the prompt ingested
# before any token comes out.
ttft = load + pe
rate = (n / ev) if ev > 0 else 0.0

print(f"  model load     {load:6.1f} s")
print(f"  first token    {ttft:6.1f} s")
if rate:
    print(f"  throughput     {rate:6.1f} tok/s  ({n} tokens in {ev:.1f} s)")
print(f"  total          {total:6.1f} s")

# Thresholds are about whether a plan step stays tolerable, not benchmarks.
# A todo turns over roughly a few hundred tokens.
if rate >= 8:
    verdict = "OK"
elif rate >= 4:
    verdict = "USABLE — a plan step will take a minute or two"
elif rate > 0:
    verdict = "SLOW — consider a smaller ctx or model"
else:
    verdict = "no timing returned"
print(f"  verdict        {verdict}")
if ttft > 30:
    print(f"  note           {ttft:.0f} s to first token; the model was cold or the")
    print( "                 context is large enough to slow prompt ingestion")
' 2>/dev/null || true

  tlc_resident_report
}
