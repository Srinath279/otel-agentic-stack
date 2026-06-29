#!/usr/bin/env bash
# End-to-end smoke test: pods healthy -> agent answers -> traffic -> telemetry landed.
# Exit code = number of failed checks (0 = all green).
set -uo pipefail

NS=otel-demo
pass=0; fail=0
ok()   { echo "  PASS  $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $1"; fail=$((fail+1)); }
check(){ if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }

echo "== 1. deployments ready =="
for d in agent kafka tempo loki prometheus grafana otel-collector-gateway otel-collector-consumer; do
  check "$d ready" "[ \"\$(kubectl -n $NS get deploy/$d -o jsonpath='{.status.readyReplicas}' 2>/dev/null)\" = 1 ]"
done

echo "== 2. agent responds =="
check "/healthz" "curl -sf localhost:30080/healthz"
ans=$(curl -s -X POST localhost:30080/chat -H 'content-type: application/json' \
      -d '{"message":"what is the weather in Tokyo"}' 2>/dev/null)
echo "  agent answer: ${ans:0:80}"
echo "$ans" | grep -qi tokyo && ok "/chat returns weather answer" || bad "/chat returns weather answer"

echo "== 3. generate load + wait for pipeline =="
./scripts/load.sh 30 >/dev/null 2>&1 || true
echo "  waiting 45s (agent 10s export -> gateway -> kafka -> consumer -> prometheus 15s scrape)..."
sleep 45

echo "== 4. telemetry landed =="
kubectl -n $NS port-forward svc/prometheus 9090 >/dev/null 2>&1 & pf1=$!
kubectl -n $NS port-forward svc/tempo 3200 >/dev/null 2>&1 & pf2=$!
sleep 4
ser=$(curl -s 'http://localhost:9090/api/v1/query?query=gen_ai_client_token_usage_total' \
      | python3 -c "import sys,json;print(len(json.load(sys.stdin)['data']['result']))" 2>/dev/null || echo 0)
[ "${ser:-0}" -gt 0 ] && ok "prometheus token metrics ($ser series)" || bad "prometheus token metrics (0 series)"
tr=$(curl -s 'http://localhost:3200/api/search?tags=service.name%3Dagentic-demo&limit=5' \
     | python3 -c "import sys,json;print(len(json.load(sys.stdin).get('traces',[])))" 2>/dev/null || echo 0)
[ "${tr:-0}" -gt 0 ] && ok "tempo traces ($tr found)" || bad "tempo traces (0 found)"
kill $pf1 $pf2 2>/dev/null

echo
echo "RESULT: $pass passed, $fail failed"
if [ "$fail" -eq 0 ]; then echo "ALL GREEN — open http://localhost:30030"; else echo "see failures above"; fi
exit "$fail"
