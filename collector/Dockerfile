# Wrapper image so the collector is built + `kind load`-ed like the agent.
# This sidesteps Docker Desktop's multi-arch attestation-manifest selection bug
# (pull-time picks an SBOM manifest with no binary -> "exec /otelcol-contrib:
# no such file or directory"). BuildKit resolves the correct arm64/amd64 base.
# 0.116.0 is a broken release artifact (binary fails to exec); 0.119.0 verified good.
FROM otel/opentelemetry-collector-contrib:0.119.0
