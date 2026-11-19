# AI Ops Platform — Complete Architecture, Microservices, Cloud & Viva Guide

> **Platform Summary**: A Kubernetes-orchestrated, multi-service **AI Operations (AI Ops) & FinOps Control Plane** for AI Agents, built with **FastAPI (Async Python 3.11)**, **PostgreSQL 16** (Database-per-Service isolation), **Docker**, and a **3-node Kubernetes (KIND) Cluster with Horizontal Pod Autoscaling (HPA)**.

![End-to-End Microservices Architecture](diagrams/01_system_architecture.png)

---

## 1. Why This Platform Exists & Overall Architecture

Modern engineering organizations deploy multiple AI agents across departments (Engineering, Support, Finance, Marketing) calling both local models (e.g., Ollama) and hosted APIs (OpenAI, Anthropic, Google). Without a dedicated control plane, teams suffer from:

1. **Zero team-level attribution** — token burn and bills are mixed together across departments and synthetic load tests.
2. **Uncontrolled cost spikes** — runaway loops or unthrottled prompts exceed budgets with no real-time circuit breaker.
3. **Opaque performance under concurrency** — single-instance LLM gateways queue requests and spike latency under bursty load.

The **AI Ops Platform** solves all three using a **3-microservice + 1 stateful multi-database architecture**:

- **3 Stateless Application Microservices**: `agent-service`, `usage-service`, and `cost-service` (horizontally scaled independently by Kubernetes HPA).
- **1 Stateful Infrastructure Database**: `postgres-db:5432` hosting three strictly isolated databases (`agentdb`, `usagedb`, `costdb`) owned by distinct database roles (`agentuser`, `usageuser`, `costuser`).
- **Control Plane UI**: Served directly from `agent-service` at [`http://localhost:8001/`](http://localhost:8001/) with 11 operational views, environment scoping (`Production | All | Load Tests`), and live currency switching (`₹ INR | $ USD`).

---

## 2. The Three Microservices & Their Responsibilities

### Why Microservices Instead of a Monolith?

1. **Independent Autoscaling**: `usage-service` simulates/executes model latency and writes usage telemetry, hitting `284%` CPU under bursts, while `cost-service` peaks lower (`212%` CPU). Microservices allow HPA to scale each bottleneck independently (`1–5`, `1–5`, `1–4` replicas) rather than cloning the entire stack equally.
2. **Fault Isolation & Graceful Degradation**: If `cost-service` restarts or becomes unreachable, `usage-service` catches the failure and still completes the inference request (`HTTP 200 OK` with `cost_id: null`) instead of dropping AI traffic.
3. **Clear Bounded Context & Data Ownership**: Agent governance, LLM execution/metering, and FinOps pricing/alerting evolve cleanly without schema coupling.

### Service-by-Service Breakdown

| Microservice | Host Port / K8s Service | Owned Database & Tables | Core Role & Key Logic | Failure / Edge Behavior |
|---|---|---|---|---|
| **`agent-service`** | `:8001` (`NodePort 30081`) / `http://agent-service:8000` | **`agentdb`** (`teams`, `team_members`, `agents`) | Entry orchestrator, Control Plane UI host (`dashboard.html`), Agent/Team CRUD, UUID `x-request-id` generator, Hard-Budget pre-check. | Blocks `INACTIVE` agents; blocks `budget_mode="hard"` at `>= 100%` with **`HTTP 429`**; returns **`HTTP 503`** if `usage-service` is down. |
| **`usage-service`** | `:8002` (`NodePort 30082`) / `http://usage-service:8000` | **`usagedb`** (`models`, `usage_records`) | Model catalog (5 seeded local + cloud models), deterministic prompt simulator (`MODEL_MODE=mock`) or live `ollama` runner, input/output token metering, trace preview logging (`executed_by`, `prompt_preview`, `response_preview`), `source` classification. | If `cost-service` fails/times out, catches the exception cleanly and returns **`200 OK`** with `cost_id: null`. |
| **`cost-service`** | `:8003` (`NodePort 30083`) / `http://cost-service:8000` | **`costdb`** (`pricing`, `costs`, `alerts`) | 5-model per-1K token USD pricing registry, 6-decimal USD cost calculation, daily token budget summation (`Asia/Kolkata` day boundary), idempotent `WARNING` (`>= 80%`) and `CRITICAL` (`>= 100%`) alert generation. | Protected by `UNIQUE(agent_id, alert_date, level)` (`uq_alert_agent_date_level`) so concurrent bursts never create duplicate alerts on the same day. |

---

## 3. Inter-Service Communication & Request Lifecycle

All internal service-to-service communication uses **asynchronous HTTP/REST (`httpx.AsyncClient`)** inside an isolated virtual network (`ai-ops-net` in Docker Compose; CoreDNS + `ClusterIP` in Kubernetes).

### Step-by-Step Synchronous Execution Chain (`POST /agents/{id}/run`)

1. **Client Request**: User (via Playground UI, Swagger `:8001/docs`, or load generator `scripts/k8s_hpa_loadtest.py`) sends `POST /agents/{id}/run` with `prompt`, optional `model_override`, `executed_by`, and `source` (`production` / `loadtest`).
2. **`agent-service` Validation & Trace Init**:

    - Generates a unique UUID **`x-request-id`** header for distributed tracing.
    - Verifies agent exists and `status == "ACTIVE"`.
    - If `agent.budget_mode == "hard"`, queries daily budget status and immediately rejects requests at `>= 100%` utilization with **`HTTP 429 Too Many Requests`** before incurring token spend.

3. **`agent-service` → `usage-service`**:

    - Forwards `POST http://usage-service:8000/usage/record` passing `x-request-id`.
    - `usage-service` executes or simulates the selected model, counts `input_tokens` and `output_tokens`, records `latency_ms`, builds `prompt_preview` & `response_preview`, and inserts a `UsageRecord` into `usagedb`.

4. **`usage-service` → `cost-service`**:

    - Wrapped in `try / except`, calls `POST http://cost-service:8000/costs/record` with `x-request-id`, `agent_id`, `usage_id`, `model`, `input_tokens`, `output_tokens`, `total_tokens`, and `daily_token_budget`.
    - `cost-service` computes USD cost, inserts a `Cost` row into `costdb`, sums `tokens_today` in `Asia/Kolkata`, and upserts `WARNING` / `CRITICAL` alerts if thresholds are crossed.

5. **Aggregated Response**: `cost-service` returns cost & alert status to `usage-service`, which returns combined usage + cost telemetry to `agent-service`, which renders/returns the unified response (all stamped with the same `request_id`).

---

## 4. PostgreSQL Architecture & Schema Mapping

![PostgreSQL Multi-DB Isolation Schema & Formulas](diagrams/04_database_and_formulas.png)

### How PostgreSQL Is Set Up (`db/init.sql` & `k8s/02-postgres.yaml`)

- **One Stateful Postgres Server, Three Isolated Databases**:
    1. **`agentdb`** (owner: **`agentuser`**) → `teams`, `team_members`, `agents`
    2. **`usagedb`** (owner: **`usageuser`**) → `models`, `usage_records`
    3. **`costdb`** (owner: **`costuser`**) → `pricing`, `costs`, `alerts`
- **No Cross-Database SQL Foreign Keys**: Even though all three databases run inside one `postgres-db:5432` instance for operational simplicity, each service only holds credentials to its own database. Links between services are **logical ID references** (`agent_id`, `usage_id`, `request_id`). Each microservice could be split onto a separate physical RDS/CloudSQL server simply by updating its `DATABASE_URL`.
- **Kubernetes Stateful Configuration**:
    - `replicas: 1` with `strategy: Recreate` (prevents two Postgres pods mounting the same persistent volume simultaneously).
    - Backed by a `1Gi` `PersistentVolumeClaim` (`postgres-pvc`) at `/var/lib/postgresql/data`.
    - Tuned for HPA pod bursts: `max_connections=250`, `shared_buffers=256MB` (Docker Compose uses `max_connections=150`).
    - **No HPA on Postgres**: Horizontally scaling stateless web pods shares compute safely, whereas blindly cloning single-primary Postgres pods causes split-brain data corruption.

### Database Tables Reference

- **`agentdb`**:
    - `teams`: Organization/department units with monthly USD budget allocation and description.
    - `team_members`: Named team operators with role and active status (`FK -> teams.id`).
    - `agents`: `id`, `name`, `model`, `owner_team`, `daily_token_budget`, `budget_mode` (`soft` | `hard`), `status`, `created_at`.
- **`usagedb`**:
    - `models`: `id`, `name`, `provider`, `is_local`, simulation parameters (5 seeded models: `qwen3.5:4b`, `llama3.1:8b`, `gemini-flash`, `gpt-4o-mini`, `claude-haiku`).
    - `usage_records`: `id`, `agent_id` (logical), `agent_name`, `model_id` (`FK -> models.id`), `input_tokens`, `output_tokens`, `total_tokens`, `latency_ms`, `status`, `source`, `request_id`, `executed_by`, `prompt_preview`, `response_preview`.
- **`costdb`**:
    - `pricing`: `id`, `model`, per-1K USD input rate, per-1K USD output rate.
    - `costs`: `id`, `pricing_id` (`FK -> pricing.id`), `usage_id` (logical), `agent_id` (logical), `agent_name`, `request_id`, `total_tokens`, `cost_usd`, `created_at`.
    - `alerts`: `agent_id`, `agent_name`, `level` (`WARNING` | `CRITICAL`), `budget_used_pct`, `tokens_today`, `daily_token_budget`, `alert_date`, `message`, `created_at` with `UNIQUE(agent_id, alert_date, level)` (`uq_alert_agent_date_level`).

---

## 5. Exact Mathematical Formulas & Pricing Matrix

### Formula 1 — Token Cost Calculation (`cost-service`) & INR Conversion (`dashboard.html`)

$$\text{Cost}_{\text{USD}} = \text{round}\!\left(\left(\frac{\text{input\_tokens}}{1000} \times P_{\text{in}}\right) + \left(\frac{\text{output\_tokens}}{1000} \times P_{\text{out}}\right),\; 6\right)$$

$$\text{Cost}_{\text{INR}} = \text{Cost}_{\text{USD}} \times 85.0 \qquad (\text{UI currency toggle: } \texttt{USD\_TO\_INR = 85.0})$$

**Seeded 5-Model Pricing Table (`cost-service/app/seed.py`)**:

| Model | Provider | Hosting | Input Price / 1K Tokens (USD) | Output Price / 1K Tokens (USD) |
|---|---|---|---:|---:|
| `qwen3.5:4b` | Ollama | Local (`is_local=true`) | `$0.000000` | `$0.000000` |
| `llama3.1:8b` | Ollama | Local (`is_local=true`) | `$0.000000` | `$0.000000` |
| `gemini-flash` | Google | Cloud | `$0.000075` | `$0.000300` |
| `gpt-4o-mini` | OpenAI | Cloud | `$0.000150` | `$0.000600` |
| `claude-haiku` | Anthropic | Cloud | `$0.000250` | `$0.001250` |

**Worked Example**: A call to `gpt-4o-mini` with `120` input tokens and `250` output tokens:

- Input cost: $(120 / 1000) \times \$0.000150 = \$0.000018$
- Output cost: $(250 / 1000) \times \$0.000600 = \$0.000150$
- Total: **`$0.000168`** (approx. **`₹0.0143 INR`** at `₹85/$1`).

---

### Formula 2 — Daily Budget Utilization & Governance (`cost-service` + `agent-service`)

$$\text{Budget Used \%} = \left(\frac{\sum \text{tokens\_today (in }\texttt{Asia/Kolkata}\text{)}}{\text{daily\_token\_budget}}\right) \times 100$$

- **Warning Threshold**: `budget_used_pct >= 80%` → emits idempotent `WARNING` alert row for `(agent_id, alert_date, "WARNING")`.
- **Critical Threshold**: `budget_used_pct >= 100%` → emits idempotent `CRITICAL` alert row for `(agent_id, alert_date, "CRITICAL")`.
- **Enforcement Modes**:
    - `soft`: Logs alerts at `80%` and `100%` while permitting continued execution.
    - `hard`: `agent-service` enforces a pre-run check and blocks further requests once at `100%` with **`HTTP 429 Too Many Requests`**.

---

### Formula 3 — Kubernetes HPA Autoscaling Math (`autoscaling/v2`)

Kubernetes HPA never uses `limits.cpu` for scaling decisions — it always divides **live pod CPU usage** (reported by Metrics Server) by **`resources.requests.cpu`**:

$$\text{Current CPU Utilization \%} = \left(\frac{\text{Actual Pod CPU (millicores)}}{\text{pod.resources.requests.cpu (250m)}}\right) \times 100$$

$$\text{desiredReplicas} = \left\lceil \text{currentReplicas} \times \left(\frac{\text{Current CPU Utilization \%}}{\text{targetAverageUtilization (50\%)}}\right) \right\rceil$$

**Live Observed Step (`T+45s` on `agent-service`)**:

- Suppose 1 replica consumes `408m` CPU out of `250m` requested $\rightarrow (408 / 250) \times 100 = 163\%$ (`target = 50%`).
- Uncapped desired replicas: $\lceil 1 \times (163 / 50) \rceil = \lceil 3.26 \rceil = 4$.
- Bounded by HPA `scaleUp` policy (`100%` or `+2` pods per `15s`) and `maxReplicas: 5`, replicas climb smoothly (`1 → 3 → 4 → 5`) as load persists.

---

## 6. Cloud Computing Concepts, Kubernetes Topology, Load Balancing & Autoscaling

![Kubernetes 3-Node KIND Cluster, Load Balancing & Resource Limits](diagrams/02_kubernetes_kind_hpa.png)

### How Core Cloud Computing Principles Map to This Project

| Cloud Computing Concept | Where & How It Is Implemented in AI Ops Platform |
|---|---|
| **Containerization & Portability** | Multi-service Python Docker images (`lab/agent-service:1.0`, `lab/usage-service:1.0`, `lab/cost-service:1.0`, `postgres:16`) run identically across Docker Compose and KIND worker nodes. |
| **Container Orchestration** | Kubernetes (`v1.37.0`) via KIND (`v0.33.0`) automates pod scheduling across multiple worker nodes, readiness/liveness health probes, self-healing restarts, ConfigMaps, Secrets, and declarative YAML rollouts (`k8s/`). |
| **Cluster Topology** | **3-node KIND cluster (`ai-ops`)**: `ai-ops-control-plane` (`172.19.0.4`), `ai-ops-worker` (`172.19.0.3`), and `ai-ops-worker2` (`172.19.0.2`). |
| **Ingress & `NodePort` Mapping** | Because KIND runs locally without a cloud `LoadBalancer` provider, [`k8s/kind-config.yaml`](../k8s/kind-config.yaml) maps `extraPortMappings` from host `localhost:8001–8003` to `NodePort 30081–30083` (`agent`, `usage`, `cost`). |
| **Internal DNS & Load Balancing** | Kubernetes CoreDNS resolves `http://usage-service:8000`, `http://cost-service:8000`, and `postgres-db:5432` to `ClusterIP` virtual IPs. `kube-proxy` distributes traffic evenly across all **Ready** pod endpoints as HPA scales replicas from `1` up to `5`. |
| **Stateless vs. Stateful Separation** | Application microservices hold zero local disk state and scale freely; PostgreSQL runs as a single pinned stateful pod backed by a `1Gi` PersistentVolumeClaim (`postgres-pvc`). |
| **Elasticity & Horizontal Autoscaling** | `Metrics Server v0.9.1` (`kube-system`, `--kubelet-insecure-tls`, `15s` scraping) feeds the `autoscaling/v2` HPA controller to expand pods dynamically under burst load (`scaleUp stabilizationWindowSeconds: 0`) and reclaim resources during idle (`scaleDown stabilizationWindowSeconds: 60`). |
| **Cloud FinOps & Multi-Tenancy** | Team entities, `team_members` attribution, `Production | All | Load Tests` environment isolation, INR/USD cost calculation, and soft/hard daily token budgets prevent cloud overspend. |

---

### Requests vs. Limits & Host Capacity Calibration

A critical Kubernetes engineering lesson demonstrated in this project is **capacity calibration**:

- **`resources.requests`**: The minimum guaranteed CPU/memory Kubernetes reserves when scheduling a pod onto a node (and the denominator for HPA CPU %).
- **`resources.limits`**: The hard Linux cgroup ceiling enforced at runtime:
    - Exceeding **CPU limit** causes **CPU throttling** (requests slow down, pod stays alive).
    - Exceeding **Memory limit** causes **`OOMKilled` (Exit Code `137`)** termination.

**Why Naive Limits Fail on a 16 GB Laptop**:

- Our physical host (`Intel Core i7-13650HX`, `20` logical CPUs, `15.78 GiB` RAM) gives Docker Desktop a Linux VM ceiling of **`7.642 GiB` (`8,012,968 Ki`) RAM**.
- An uncalibrated `500m CPU / 512Mi RAM` request across 14 max app pods plus `500m / 1Gi` for Postgres adds up to **`7,500m` CPU and `8,192 MiB` (`8.0 GiB`) RAM requests** — exceeding the `7.642 GiB` VM and causing pods to stall in **`Pending`**!
- **Calibrated Resource Allocation Deployed in [`k8s/`](../k8s/)**:

| Component | Min / Max Replicas | CPU Request / Limit | Memory Request / Limit | Max Total Requests |
|---|---:|---|---|---|
| `agent-service` | `1 / 5` | `250m` / `2000m` | `256Mi` / `1Gi` | `1,250m` CPU, `1,280Mi` RAM |
| `usage-service` | `1 / 5` | `250m` / `2000m` | `256Mi` / `1Gi` | `1,250m` CPU, `1,280Mi` RAM |
| `cost-service` | `1 / 4` | `250m` / `1500m` | `256Mi` / `1Gi` | `1,000m` CPU, `1,024Mi` RAM |
| `postgres-db` | `1 / 1` (No HPA) | `500m` / `2000m` | `1Gi` / `2Gi` | `500m` CPU, `1,024Mi` RAM |
| **Max Cluster Total** | **15 pods** | — | — | **`4,000m` CPU, `4.5 GiB` RAM** |

This guarantees headroom (~3 GiB inside the Docker VM for control-plane & `kube-system` pods) while providing 4×–15× each pod's measured baseline footprint (`~60–72 MiB` app pods, `~90–216 MiB` Postgres).

---

## 7. Measured HPA Autoscaling & Benchmark Results

![Verified Live KIND HPA Autoscaling & Latency Response](diagrams/03_hpa_scaling_timeline.png)

### Evaluation 2 — Live KIND HPA Load Test (`scripts/k8s_hpa_loadtest.py`, Concurrency = 48)

1. **Baseline Idle (`1 / 1 / 1`)**: `agent-service 0–2%` (`2m`), `usage-service 0–4%` (`2m`), `cost-service 0–3%` (`2m`), `postgres-db 7–10m` CPU.
2. **Scale-Up Run 1 (`duration=75.4s`, `5,796` requests, `0` errors, `avg 538.0 ms`, `p95 575.5 ms`, `76.9 rps`)**:
    - `T+45s`: `agent` `163%/50%` (`408m`) → `3`, `usage` `108%/50%` (`272m`) → `3`, `cost` `84%/50%` (`211m`) → `2`.
    - `T+60s`: `agent` `240%/50%` (`601m`), `usage` `284%/50%` (`711m`), `cost` `212%/50%` (`530m`), `postgres` `422m / 165Mi`.
    - `T+75s`: All three hit maximum scale (**`5 / 5 / 4` = `15` running pods**, `0` restarts, `0` `Pending`, `0` `OOMKilled`). Combined 3-node memory stayed at **`~2.88 GiB / 7.642 GiB`** (`control-plane 939.3 MiB`, `worker 952 MiB`, `worker2 987.3 MiB`).
3. **Latency Drop During Scale-Up (Run 2: `duration=66.0s`, `4,258` requests, `0` errors)**:
    - As newly scaled replicas passed readiness checks and entered `ClusterIP` rotation, window average latency dropped from **`808.6 ms`** (`t=36s`) down to **`431.6 ms`** (`t=51s`, `111.1 rps`).
4. **Automatic Scale-Down After Traffic Stops**:
    - `T+60s` cooldown: `5/5/4 → 3/3/2`.
    - `T+100s`: Returned cleanly to **`1 / 1 / 1`** (`0–1%` CPU), and post-scale-down `POST /agents/1/run` verified `status=success`.

### Evaluation 1 — Single-Container Docker Compose Concurrency Baseline (`results/ANALYSIS.md`)

Across `W1–W5` (`400` requests/level × 3 repeats = `6,000` total requests, `MODEL_MODE=mock`):

- **W1 (Concurrency 1)**: `avg 108.2 ms`, `p95 134.6 ms`, `9.240 rps`, `0` infra failures (`85.8 ms` model + `22.4 ms` overhead).
- **W3 (Concurrency 4)**: `avg 109.2 ms`, `p95 135.4 ms`, `36.340 rps`, `0` infra failures.
- **W5 (Concurrency 16)**: `avg 121.4 ms`, `p95 150.5 ms`, `127.640 rps`, `0` infra failures (`+12%` latency from single-uvicorn queueing; peak CPU `usage-service 62.52%`, `cost-service 51.01%`, `agent-service 48.75%`, `postgres-db 32.61%`).
- Supporting plots remain stored in `results/01_response_time.png` through `results/07_latency_breakdown.png`.

---

## 8. Enterprise Control Plane UI (`agent-service/app/dashboard.html`)

Accessed at [`http://localhost:8001/`](http://localhost:8001/), the single-page dashboard spans **11 views**:
`overview`, `agents`, `agent-detail`, `teams`, `models`, `playground`, `create-agent`, `observability`, `costs`, `alerts`, and `infrastructure`.

**Key Capabilities Implemented**:

- **Environment Scope Filter**: Top-bar `Production | All | Load Tests` toggle isolates genuine team runs (e.g., `281` reqs / `73,322` tokens / `$0.028662`) from synthetic benchmark bursts (`10,054` reqs / `3,016,667` tokens / `$0.901248`) across Overview, Agents, Costs, and Observability.
- **Live Currency Switcher**: Toggles every metric, table, and card seamlessly between **`₹ INR`** (`USD * 85.0`) and **`$ USD`**.
- **`Teams & Members` Management**: Full PostgreSQL CRUD (`/teams`, `/teams/{id}/members`) for department budgets, member roles, and team attribution folders.
- **`Playground`**: Interactive prompt runner supporting **inline `model_override` switching**, **`executed_by` operator tagging**, and live **`prompt_preview` / `response_preview`** inspection.
- **Budget Mode Policy Selector**: Create/Edit Agent lets admins choose **`Soft`** (alert at 80%/100%, allow execution) or **`Hard`** (block execution at 100% with **`HTTP 429`**), plus displays the live **How Cost Is Calculated** formula banner in Costs.

---

## 9. Step-by-Step Verification & Viva Demo Cheat Sheet

Run these commands directly from the project root (`ai-ops-eval-2`) during viva presentation:

```powershell
# 1. Show 3-node KIND cluster & running pods (3 microservices + 1 stateful Postgres)
kubectl get nodes -o wide
kubectl get pods -o wide

# 2. Show HPA policies & live resource consumption
kubectl get hpa
kubectl top nodes
kubectl top pods

# 3. Verify Metrics Server & Service Discovery (ClusterIP + NodePorts 30081-30083)
kubectl get svc

# 4. Watch HPA scale out LIVE in terminal 1 while bursting load in terminal 2
kubectl get hpa -w
python scripts/k8s_hpa_loadtest.py --url http://localhost:8001 --concurrency 48 --duration 70

# 5. Inspect 3 isolated PostgreSQL databases directly inside KIND postgres pod
kubectl exec -it deploy/postgres-db -- psql -U agentuser -d agentdb -c "SELECT id, name, owner_team, budget_mode FROM agents ORDER BY id;"
kubectl exec -it deploy/postgres-db -- psql -U usageuser -d usagedb -c "SELECT source, COUNT(*) AS reqs, SUM(total_tokens) AS tokens FROM usage_records GROUP BY source;"
kubectl exec -it deploy/postgres-db -- psql -U costuser -d costdb -c "SELECT agent_name, level, budget_used_pct, alert_date FROM alerts ORDER BY created_at DESC LIMIT 10;"
```

Also available: [`db/viva_queries.sql`](../db/viva_queries.sql) (15 ready-to-run SQL queries across `agentdb`, `usagedb`, `costdb`) and [`docs/DEMO_CHECKLIST.md`](DEMO_CHECKLIST.md).

---

## 10. Project Advantages & Rapid-Fire Viva Q&A

### Key Architectural Advantages

1. **True End-to-End Traceability**: A single `x-request-id` correlates user actions in UI → `agent-service` → `usage-service` → `cost-service` → PostgreSQL rows.
2. **Real FinOps Controls**: Combines exact 6-decimal token cost accounting, INR localization, team attribution, environment scoping, and hard `HTTP 429` budget circuit breaking.
3. **Production-Style Resilience**: `cost-service` failures degrade gracefully (`cost_id: null`), alert upserts are race-safe (`UNIQUE(agent_id, alert_date, level)`), and stateful storage stays decoupled from stateless HPA replicas.
4. **Right-Sized Cloud Engineering**: Resource requests (`250m / 256Mi`) and limits (`2000m / 1Gi`) were empirically tuned to fit host VM RAM while still triggering fast Kubernetes HPA scaling (`1 → 5/5/4 → 1/1/1`).

### Quick Viva Q&A Summary

- **Q: Why use 3 databases inside 1 Postgres pod instead of 1 shared database or 3 Postgres pods?**  
  A: Separate schemas/users (`agentdb`, `usagedb`, `costdb`) enforce microservice data ownership and forbid cross-DB SQL joins (using only logical IDs), while keeping baseline RAM low (`~60–216 MiB`) inside one persistent Postgres volume (`1Gi PVC`).

- **Q: Why does Kubernetes HPA calculate `%` using `requests.cpu` instead of `limits.cpu`?**  
  A: Requests define guaranteed baseline capacity. Comparing real CPU (`408m`) against `250m` request gives `163%`, triggering scale-up above the `50%` target well before pods hit the `2000m` throttle ceiling.

- **Q: Why is there NO HPA on `postgres-db`?**  
  A: Relational primary databases are stateful; naive replica cloning against a single `1Gi` PVC would corrupt writes or cause split-brain state. Instead, Postgres scales vertically (`500m–2000m` CPU, `1Gi–2Gi` RAM, `max_connections=250`).

- **Q: How are requests load-balanced when HPA scales `usage-service` from 1 pod to 5 pods?**  
  A: `agent-service` calls the Kubernetes CoreDNS name `http://usage-service:8000`. As each new pod passes its readiness probe, Kubernetes adds its pod IP to the service Endpoints and `kube-proxy` distributes incoming connections across all 5 pods automatically.

- **Q: How do you prevent synthetic load-test traffic from inflating company FinOps dashboards?**  
  A: Requests carry `source="loadtest"` vs `source="production"`. The UI defaults to `Production` (`281` reqs / `$0.028662`), while operators can switch to `Load Tests` (`10,054` reqs / `$0.901248`) or `All` in one click.
