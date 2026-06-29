SHELL := /bin/bash
-include .env
export

CLUSTER := otel-demo
IMAGE := agentic-demo:dev
NS := otel-demo
KREP := kubectl -n $(NS)

.PHONY: up down cluster image collector-image secret dashboard backends collectors agent load urls logs reload verify multi

up: cluster image collector-image secret backends collectors agent ## Full bring-up
	@echo
	@$(MAKE) urls

cluster: ## Create the kind cluster (idempotent)
	@kind get clusters | grep -qx $(CLUSTER) || kind create cluster --config kind/cluster.yaml
	@kubectl apply -f k8s/00-namespace.yaml

image: ## Build the agent image and load it into kind (single-arch + archive load)
	docker build --provenance=false --sbom=false -f agent/Dockerfile -t $(IMAGE) .
	docker save $(IMAGE) -o /tmp/agentic-demo.tar
	kind load image-archive /tmp/agentic-demo.tar --name $(CLUSTER)

secret: ## Create the Anthropic secret from .env (only if a key is set)
	@if [ -n "$$ANTHROPIC_API_KEY" ]; then \
	  $(KREP) create secret generic anthropic \
	    --from-literal=ANTHROPIC_API_KEY="$$ANTHROPIC_API_KEY" \
	    --dry-run=client -o yaml | kubectl apply -f - ; \
	  echo "anthropic secret applied"; \
	else echo "No ANTHROPIC_API_KEY (mock mode)"; fi

dashboard: ## (Re)load all Grafana dashboards from dashboards/
	@$(KREP) create configmap grafana-dashboard \
	  --from-file=dashboards/ \
	  --dry-run=client -o yaml | kubectl apply -f -

backends: dashboard ## Kafka + Tempo + Loki + Prometheus + Alertmanager + Grafana
	kubectl apply -f k8s/10-kafka.yaml -f k8s/20-tempo.yaml -f k8s/21-loki.yaml \
	  -f k8s/22-prometheus.yaml -f k8s/24-alertmanager.yaml -f k8s/23-grafana.yaml
	$(KREP) rollout status deploy/kafka --timeout=180s
	$(KREP) rollout status deploy/tempo --timeout=120s
	$(KREP) rollout status deploy/prometheus --timeout=120s
	$(KREP) rollout status deploy/grafana --timeout=120s

collector-image: ## Build + load the collector image into kind (single-arch; archive load works with Docker Desktop containerd store)
	docker build --provenance=false --sbom=false -f collector/Dockerfile -t otelcol-local:dev collector
	docker save otelcol-local:dev -o /tmp/otelcol-local.tar
	kind load image-archive /tmp/otelcol-local.tar --name $(CLUSTER)

collectors: collector-image ## Gateway + Consumer collectors
	kubectl apply -f k8s/30-collector-gateway.yaml -f k8s/31-collector-consumer.yaml
	$(KREP) rollout status deploy/otel-collector-gateway --timeout=300s
	$(KREP) rollout status deploy/otel-collector-consumer --timeout=300s

agent: ## Deploy the agent (applies LLM_MODE from .env if set)
	@if [ -n "$$LLM_MODE" ]; then \
	  $(KREP) patch configmap agent-config --type merge \
	    -p "{\"data\":{\"LLM_MODE\":\"$$LLM_MODE\"}}"; fi
	kubectl apply -f k8s/40-agent.yaml -f k8s/50-hpa.yaml -f k8s/60-networkpolicy.yaml
	$(KREP) rollout status deploy/agent --timeout=120s

reload: image ## Rebuild + roll the agent (dev loop)
	$(KREP) rollout restart deploy/agent
	$(KREP) rollout status deploy/agent --timeout=120s

load: ## Fire traffic at the agent
	./scripts/load.sh $(N)

verify: ## End-to-end smoke test (pods, agent, metrics, traces) -> pass/fail
	./scripts/verify.sh

multi: image ## Add a 2nd agent (support-agent) + sub-agent endpoint + in-cluster load
	$(KREP) rollout restart deploy/agent
	kubectl apply -f k8s/41-agents-extra.yaml
	$(KREP) rollout status deploy/agent --timeout=120s
	$(KREP) rollout status deploy/support-agent --timeout=120s
	$(KREP) rollout status deploy/loadgen --timeout=60s
	@echo "Multi-agent running. Grafana -> 'Agentic — Multi-Agent & Sub-Agents'"

urls: ## Print access URLs
	@echo "Grafana:  http://localhost:30030  (anonymous admin)"
	@echo "Agent:    http://localhost:30080  (POST /chat)"
	@echo "Try:      make load"

logs: ## Tail the agent logs
	$(KREP) logs -l app=agent -f --tail=50

down: ## Delete the cluster
	kind delete cluster --name $(CLUSTER)
