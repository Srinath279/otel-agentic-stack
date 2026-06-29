# Helm path (production deploy shape)

The runnable demo uses raw manifests in `k8s/` so `make up` is self-contained. In production you'd
deploy the **same two collector configs** via the upstream chart, one release per tier — exactly the
DoorDash "one collector image + per-tier `values.yaml`" model.

```bash
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm repo update

# Gateway tier (stateless, autoscaled)
helm upgrade --install otel-gateway open-telemetry/opentelemetry-collector \
  -n otel-demo -f helm/values-gateway.yaml

# Consumer tier (stateful: tail sampling)
helm upgrade --install otel-consumer open-telemetry/opentelemetry-collector \
  -n otel-demo -f helm/values-consumer.yaml
```

The `config:` block in each values file is the same collector config embedded in
`k8s/30-collector-gateway.yaml` / `k8s/31-collector-consumer.yaml`. Keep them in sync, or make the
Helm values the single source of truth and drop the raw collector ConfigMaps.

For the app side, prefer the **OpenTelemetry Operator** so instrumentation + a sidecar collector are
injected by a single pod annotation (`instrumentation.opentelemetry.io/inject-python: "true"`) — zero
app code change. See PLAN.md §7.5.
