#!/usr/bin/env bash
# Drive the agent so the pipeline has live telemetry.
set -euo pipefail

URL="${AGENT_URL:-http://localhost:30080}/chat"
N="${1:-50}"

prompts=(
  "What is the weather in Tokyo?"
  "Calculate 23 * (4 + 6)"
  "What is the weather in for Berlin?"
  "Compute 99 / 9"
  "Tell me a fact about otters"
  "weather in Paris"
)

echo "Sending $N requests to $URL"
for i in $(seq 1 "$N"); do
  p="${prompts[$((RANDOM % ${#prompts[@]}))]}"
  curl -s -X POST "$URL" -H 'content-type: application/json' \
    -d "{\"message\": \"$p\"}" >/dev/null && echo -n "." || echo -n "x"
  sleep 0.3
done
echo
echo "Done. Open Grafana at http://localhost:30030"
