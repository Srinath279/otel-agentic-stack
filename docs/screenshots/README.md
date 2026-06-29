# Dashboard screenshots

Drop PNGs here and they render in the main README. Suggested filenames (referenced by the README):

| File | Dashboard |
|---|---|
| `overview.png` | Agentic — OTEL Overview |
| `cost-usage.png` | Agentic — Cost & Token Usage |
| `performance.png` | Agentic — Performance & Latency |
| `pipeline-health.png` | Agentic — Pipeline Health |
| `multi-agent.png` | Agentic — Multi-Agent & Sub-Agents |
| `trace-tree.png` | A coordinator→sub-agent trace in Explore → Tempo |

## How to capture

1. Bring up the stack and generate traffic:
   ```bash
   make up && make multi      # multi adds the 2nd agent + continuous load
   open http://localhost:30030
   ```
2. In Grafana, open each dashboard (☰ → Dashboards), set the time range to **Last 30 minutes**.
3. Use your OS screenshot tool (macOS: ⌘⇧4) or the browser's full-page capture, and save into this
   folder with the filename above.
4. For the trace tree: Explore → **Tempo** → search `service.name=agentic-demo`, open an
   `/orchestrate` trace, and screenshot the span tree (coordinator → weather-agent / math-agent).

> These aren't committed by default (they're environment-specific). Add your own once the stack is up.
