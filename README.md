# OCE — On-Call Engineer

**A fully local autonomous SRE team (in development).** OCE monitors a company's private infrastructure, detects incidents, diagnoses the root cause, develops a fix, proves it in an isolated sandbox, and deploys it under deterministic policy control — without source code, logs, credentials, or infrastructure data ever leaving the machine.

Everything runs on a single ASUS Ascent GX10 (NVIDIA GB10, 128 GB unified memory) using Ollama to serve a shared local NVIDIA Nemotron model to three specialized agents.

> **When your infrastructure fails, your AI infrastructure shouldn't fail with it.**

---

## Table of contents

1. [Why local](#1-why-local)
2. [Design principles](#2-design-principles)
3. [System architecture](#3-system-architecture)
4. [Components](#4-components)
   - [Main Process / API](#41-main-process--api)
   - [Database](#42-database)
   - [Health status scripts](#43-health-status-scripts)
   - [LLM agents](#44-llm-agents)
   - [Sandbox](#45-sandbox)
   - [Command check gate](#46-command-check-gate)
   - [Backup system](#47-backup-system)
   - [Run executor](#48-run-executor)
   - [Slack integration](#49-slack-integration)
   - [Frontend portal](#410-frontend-portal)
5. [Incident lifecycle](#5-incident-lifecycle)
6. [Data model](#6-data-model)
7. [API reference](#7-api-reference)
8. [LLM scheduling and VRAM](#8-llm-scheduling-and-vram)
9. [Fake company (`fakeco/`)](#9-fake-company-fakeco)
10. [Demo flow](#10-demo-flow)
11. [Repository layout](#11-repository-layout)
12. [Tech stack](#12-tech-stack)
13. [Work split and roadmap](#13-work-split-and-roadmap)
14. [Open questions](#14-open-questions)

---

## 1. Why local

When production goes down, engineers routinely paste some of their company's most sensitive data — source code, logs, config, architecture, security findings — into cloud AI services to get help. OCE removes that dependency entirely.

| Reason      | What it means for OCE                                                                 |
| ----------- | ------------------------------------------------------------------------------------- |
| **Privacy** | Source code, logs, credentials, telemetry, and vulnerabilities never leave the box.   |
| **Latency** | Agents react to an incident immediately — no API round trips.                         |
| **Offline** | Incident response keeps working when Internet or cloud AI access is down.             |
| **Cost**    | No per-token charges while continuously monitoring infrastructure.                    |
| **Control** | The company owns the entire inference + remediation pipeline.                         |

The dashboard displays this prominently:

```
LOCAL AI
Hardware                      ASUS Ascent GX10
Model                         NVIDIA Nemotron
Inference                     100% LOCAL
Cloud LLM requests            0
Infrastructure data uploaded  0 bytes
```

---

## 2. Design principles

1. **Software detects, AI reasons.** Deterministic health scripts decide *that* something is wrong. The LLM decides *why* and *what to do about it*. No LLM stares at metrics.
2. **AI decides what should happen. Deterministic policy decides what AI is allowed to make happen.** The LLM never receives unrestricted shell access. Every action passes through the command check gate, which only runs pre-configured, allowlisted commands.
3. **Prove it before you ship it.** A candidate fix must reproduce the incident and then resolve it in an isolated sandbox before it is eligible for deployment.
4. **Always have a way back.** A backup is taken before every change, and every deployment can be restored from the dashboard.
5. **One model, three real agents.** Investigator, Engineer, and Guardian share a single locally served Nemotron model, but each has its own system prompt, context, tool permissions, and bounded observe–act agent loop. A prompt alone is not an agent.
6. **The API is the product.** The frontend portal is just one client. Health scripts, Slack, the LLM agents, and any external tools all talk to the same Main Process API, so local and non-local interfaces are both supported.

---

## 3. System architecture

The following is the **target architecture**, not a claim that every component is implemented today. The agent code lives in the top-level `models/` Python package; the company-under-test lives in `fakeco/`.

```text
                    ASUS ASCENT GX10 (GB10 · 128 GB unified memory)
┌────────────────────────────────────────────────────────────────────────┐
│ Browser dashboard / API / health checks / optional Slack integration   │
│                              │                                         │
│                     Main Process / API                                 │
│                              │                                         │
│                     Incident state (SQLite)                            │
│                              │                                         │
│                    models/orchestrator.py                              │
│                              │                                         │
│       ┌──────────────────────┼──────────────────────┐                  │
│       ▼                      ▼                      ▼                  │
│  INVESTIGATOR            ENGINEER               GUARDIAN              │
│  Read-only tools         Sandbox-only edits     Review-only tools     │
│  Logs, metrics, code     Patch, tests           Diff, tests, risk      │
│       │                      │                      │                  │
│       └──────────────────────┼──────────────────────┘                  │
│                              │                                         │
│                  models/core/agent.py (agent loop)                      │
│                  models/core/tool_registry.py                           │
│                              │                                         │
│               models/core/llm.py → Ollama :11434                       │
│                              │                                         │
│                     nemotron3:33b (initial)                            │
│                                                                        │
│  Observability       fakeco production       Isolated sandbox         │
│  Docker / Prometheus     Docker Compose      Docker Compose clone     │
│                                                                        │
│  Guardian recommendation → deterministic command gate → backup         │
│                          → executor → production                       │
└────────────────────────────────────────────────────────────────────────┘
```

**Control boundaries:** An LLM chooses among registered tools; Python validates arguments, enforces per-agent tool allowlists, and records the results. Investigator cannot write. Engineer can write only to a sandbox worktree. Guardian can review but cannot deploy. The deterministic command gate and executor—not the LLM—control production changes. In the initial implementation, deployment should require explicit human approval.

**Local model inventory observed on the GX10:**

| Ollama model | Approximate disk size | Role |
| --- | ---: | --- |
| `nemotron3:33b` | 27 GB | Initial shared model for all three agents |
| `nemotron-3-super:latest` | 86 GB | Larger Nemotron candidate to benchmark later |
| `qwen3.6:35b` | 23 GB | Alternative model for evaluation |
| `gemma4:26b` | 17 GB | Alternative model for evaluation |

Ollama's `/api/tags` confirms these models are installed, **not** that inference or agent tool-calling has been tested successfully. The hackathon's stated Nemotron 3.5 Lightning requirement should be checked against the actual available model/tag with organizers before the final demo.

---

## 4. Components

### 4.1 Main Process / API

The central service. Owns the database, exposes the REST API, runs the health-check scheduler, orchestrates the incident pipeline, and schedules LLM work.

Responsibilities:

- Serve the REST API (see [§7](#7-api-reference)) to the frontend, Slack, health scripts, and external clients.
- Run registered health checks on a timer and open tickets when they fail.
- Drive each ticket through the [incident lifecycle](#5-incident-lifecycle).
- Schedule bounded local LLM requests (see [§8](#8-llm-scheduling-and-vram)); begin sequentially.
- Route every proposed action to the command check gate; never execute anything directly.
- Append every state change to the ticket's event timeline so the dashboard and logs are always derivable from the DB.

Implementation: Python, FastAPI, async. Single process; background tasks for the scheduler and pipeline.

### 4.2 Database

SQLite for the hackathon (single file, zero setup, ARM-friendly). Schema is in [§6](#6-data-model). Swappable to Postgres later since all access goes through one data layer.

### 4.3 Health status scripts

Deterministic, pluggable checks. **No LLM involved.** Each check implements the same interface:

```python
class HealthCheck:
    name: str
    service: str
    interval_s: int

    def run(self) -> CheckResult:
        ...

@dataclass
class CheckResult:
    ok: bool
    service: str
    detail: str              # human-readable summary
    metrics: dict[str, float] # e.g. {"error_rate": 0.387, "p95_ms": 2240}
```

Initial checks:

| Check                    | What it does                                                                  | Opens a ticket when                   |
| ------------------------ | ----------------------------------------------------------------------------- | ------------------------------------- |
| `endpoint_check`         | HTTP GET on each service's `/health`                                          | non-2xx or timeout                    |
| `log_error_parse`        | Tails service logs, computes error rate over a sliding window                 | error rate > threshold                |
| `metrics_check`          | Scrapes Prometheus (`/metrics`) for error rate and P95 latency                | value exceeds stored baseline × factor|
| `slack_ticket_reader`    | Polls a Slack channel for incident reports                                    | a new report message appears          |

The Main Process stores the result of every run in `health_checks` (latest state) and opens a ticket with the triggering `CheckResult` attached as the first event. Thresholds and baselines live in `settings`.

### 4.4 LLM agents

The AI implementation is contained in `models/` and comprises **three separate tool-using agents**, not just three prompt templates. Each loads a role-specific prompt from `models/prompts/`, configures its permitted tools from `models/tools/`, and uses the shared bounded execution loop in `models/core/agent.py` to repeatedly decide, call a tool, observe results, and either continue or return an output.

| Agent | Source + prompt | Responsibilities | Tool access | Forbidden |
| --- | --- | --- | --- | --- |
| **Investigator** (formerly Investigator) | `agents/investigator.py` + `prompts/investigator.txt` | Examine Docker logs, Prometheus metrics, container state and relevant code; report suspected root cause with evidence | Read-only `docker_tools`, `prometheus_tools`, `filesystem_tools` | Editing files, commands with side effects, deployment |
| **Engineer** (formerly Engineer) | `agents/engineer.py` + `prompts/engineer.txt` | Create candidate patches, modify a sandbox copy, run tests and iterate | Read access to relevant code, **sandbox-scoped** `sandbox_tools` | Editing production, deployments, unrestricted shell |
| **Guardian** (formerly Guardian) | `agents/guardian.py` + `prompts/guardian.txt` | Inspect proposed diff, test evidence, operational/security risk and policy constraints; recommend approve/reject/needs testing | Read-only diff/test-result inspection and risk-review tools | Modifying patches, bypassing gate, deploying |

**Shared implementation:**

- `models/config.py` — local model name, Ollama URL and timeouts.
- `models/core/llm.py` — client for Ollama's local `/api/chat` endpoint.
- `models/core/agent.py` — bounded tool-call → execution → observation loop, with timeouts and error handling.
- `models/core/tool_registry.py` — typed tool schemas, argument validation, per-agent allowlists and execution dispatch.
- `models/core/state.py` — incident context, observations and evidence exchanged between steps.
- `models/orchestrator.py` — Investigator → Engineer → Guardian handoffs, retries and escalation.
- `models/main.py` — agent subsystem entry point.

A **shared model does not mean shared permissions**: each agent has its own prompt, execution history, and allowed tools. Tool permissions are enforced in code and infrastructure, not only in system prompts. Tool outputs such as logs and repository contents are untrusted data and must not be treated as instructions.

**Expected agent outputs (planned contracts):**

- Investigator: structured diagnosis, evidence references, confidence and unresolved questions.
- Engineer: proposed patch/diff, sandbox reproduction evidence, test commands and before/after results.
- Guardian: risk analysis, policy violations, decision (`APPROVE`, `REJECT`, `NEEDS_TESTING`) and justification. An `APPROVE` is advisory, **not** permission to deploy.

The sandbox, command gate, backup and executor described below are separate components. The directory structure is already created; the agent loops, tool implementations, state persistence and integrations must still be built and tested.

### 4.5 Sandbox

An isolated copy of production where the Engineer works and where fixes are proven.

Mechanism:

1. Create a git worktree of the `fakeco/` repo at the currently deployed commit.
2. Start a second Docker Compose project (`docker compose -p sandbox`) from that worktree on separate ports / network.
3. Inject the same failure that production is experiencing (replay the bad config/commit).
4. Run the load generator → record **BEFORE** metrics. The incident must reproduce, or the diagnosis is sent back to Diag.
5. Engineer applies its patch via `modify_sandbox()`.
6. Run the load generator again → record **AFTER** metrics. Run the test suite.
7. Compare AFTER against the stored healthy baseline. All of this is attached to the ticket as evidence.

```
                BASELINE      BEFORE FIX     AFTER FIX
Error rate       0.20%         38.12%         0.17%  ✓
P95              180 ms        2188 ms        186 ms ✓
Tests            PASS          —              PASS   ✓
```

The sandbox is torn down after the ticket closes. The Engineer has no network path to the production stack.

### 4.6 Command check gate

The single choke point between AI and the real system. **Deterministic, no LLM.**

Every action request has the shape:

```json
{
  "action": "deploy_patch",
  "args": { "incident": "INC-0042", "patch": "FIX-001" },
  "requested_by": "guardian",
  "evidence": { "sandbox_run_id": "...", "risk_score": 8 }
}
```

The gate checks, in order:

```
┌─ POLICY ENGINE ──────────────────────────┐
│ 1. Action is on the allowlist?        ✓  │
│ 2. Args match the action's schema?    ✓  │
│ 3. Args within allowed values?        ✓  │  (e.g. only whitelisted config keys)
│ 4. Sandbox reproduced + fixed?        ✓  │
│ 5. Tests passed?                      ✓  │
│ 6. Risk score below threshold?        ✓  │
│ 7. Backup taken successfully?         ✓  │  (gate calls Backup system itself)
│ 8. Not on the blocklist / cooldown?   ✓  │
└──────────────────────────────────────────┘
          → APPROVED → Run executor
          → DENIED   → ticket event + dashboard, waits for human
```

The allowlist is a YAML file of **named, parameterised commands** — the LLM never writes raw shell:

```yaml
actions:
  restart_service:
    args: { service: { enum: [orders-api, frontend] } }
    requires: [backup]
  set_config:
    args:
      service: { enum: [orders-api] }
      key:     { enum: [DB_TIMEOUT, POOL_SIZE] }
      value:   { type: int, min: 100, max: 10000 }
    requires: [backup, sandbox_pass]
  deploy_patch:
    args: { incident: { type: str }, patch: { type: str } }
    requires: [backup, sandbox_pass, tests_pass, risk_below: 30]
  rollback:
    args: { deployment_id: { type: str } }
    requires: [backup]
```

Everything the gate does — approved or denied — is written to `ticket_events` and, for approvals, to `deployments`.

### 4.7 Backup system

Called by the gate before any approved action runs. For the hackathon:

- Tag the current `fakeco/` commit (`backup/<deployment_id>`).
- Snapshot the running config (env, compose state) to `backups/<deployment_id>/`.
- Record the backup reference in `deployments`.

**Restore** = check out the tag, re-apply the snapshot, redeploy. Exposed as `POST /deployments/{id}/restore` and as the **Restore Backup** button on the Past Deployments page. Also triggered automatically if post-deploy health checks get worse.

### 4.8 Run executor

Executes an approved action against the production stack. It knows how to perform each allowlisted action (and nothing else).

Deployment is staged and monitored:

```
snapshot → canary → monitor → widen → monitor → 100% → re-run health checks
```

For the hackathon, "canary percentages" are visual stages; real traffic splitting can be added with nginx upstream weights if time allows. After the run completes, the health checks re-run. If the triggering check passes, the ticket is resolved; if things are worse, the executor calls Restore.

### 4.9 Slack integration

Two directions:

- **Inbound:** `slack_ticket_reader` health check polls a channel; a matching message creates a ticket (`source: slack`).
- **Outbound:** the Main Process posts ticket state changes (detected, diagnosed, fix proposed, awaiting approval, deployed, resolved) to a channel.

Optional for the demo; the system is fully functional without Slack.

### 4.10 Frontend portal

A web dashboard that consumes the REST API. Pages:

```
Login
  └─ Main Dash ─────────────── LOCAL AI panel (hardware, model, 0 cloud requests, 0 bytes uploaded)
       ├─ Current Tickets      live incident list, timeline per ticket, approve/deny for gated actions
       ├─ Past Deployments     every executed action, with evidence → [Restore Backup]
       ├─ Logs Display         ticket events + service logs
       ├─ Overall Sys Health   per-service status from health checks
       │    └─ Health Check Scripts   registered checks, last result, enable/disable
       └─ Settings             thresholds, baselines, allowlist, Slack config
```

#### Hosting, data, and auth — decision: fully local

Supabase + Vercel + Auth0 were considered for the dashboard and rejected. All three are cloud services, and the dashboard would contradict the project's core claims:

- **Supabase** would hold tickets, diagnoses, and logs in a cloud Postgres — that *is* the infrastructure data the "0 bytes uploaded" panel says never leaves the box.
- **Vercel** would host the UI off-box, so the Internet-outage demo would take down OCE's own console.
- **Auth0** would make login depend on an external service during an outage.

What we use instead:

| Need            | Cloud option  | Local choice                                                                                         |
| --------------- | ------------- | ---------------------------------------------------------------------------------------------------- |
| Dashboard host  | Vercel        | Server-rendered by the Main Process itself (Jinja2 templates). No separate frontend service.         |
| Data + realtime | Supabase      | OCE's own REST API (§7) + an SSE stream of `ticket_events` for live updates                          |
| Login           | Auth0         | Local admin account(s), hashed password in `settings`, session cookie issued by the API. Stretch: self-hosted Authentik/Keycloak for SSO |

**Remote access (opt-in, off for the demo):** the API supports non-local clients, so a hosted UI could talk to the GX10 over a tunnel for remote operators. It is never the system of record — all state stays in the local DB.

#### Frontend stack: HTMX + Jinja2 + Alpine.js

No SPA framework and **no Node toolchain**. The dashboard is HTML rendered by FastAPI.

| Piece          | Role                                                                                              |
| -------------- | ------------------------------------------------------------------------------------------------- |
| **Jinja2**     | Page and fragment templates, rendered by FastAPI. Templates live next to the routes.              |
| **HTMX**       | Partial page updates (`hx-get`/`hx-post` on buttons, tables, and forms). The SSE extension subscribes to `/tickets/{id}/stream` and swaps new timeline events into the page as agents work. |
| **Alpine.js**  | Client-side widgets: dropdowns, modals (confirm approve/deny/restore), tabs, toggles.             |
| **Chart.js**   | Error rate and P95 charts, fed from `/tools/metrics`. Vendored locally.                           |
| **Pico CSS** or **Tailwind (CLI build, vendored output)** | Styling. Pico needs no build at all.                               |

All JS/CSS is vendored into `oce/static/` so the dashboard works with the Internet disconnected.

Why this fits:

- The pages are mostly tables and detail views (tickets, deployments, logs, health checks) with a few actions. That is what HTMX is built for.
- Live agent output is the heart of the demo; HTMX SSE gives that in one attribute, no WebSocket client code.
- The same person owns the API and the UI, so there is no frontend/backend contract to negotiate.
- One fewer container on the GX10, nothing to build for ARM64.

Route conventions:

```
GET /                      → redirect to /dash (or /login)
GET /login, POST /login    → session cookie
GET /dash                  → full page
GET /tickets               → full page; with HX-Request header returns only the table fragment
GET /tickets/{id}          → full page; timeline fragment at /tickets/{id}/timeline
GET /tickets/{id}/stream   → text/event-stream of rendered timeline rows
POST /tickets/{id}/approve → returns updated gate-status fragment
GET /deployments, /logs, /health, /settings → same full-page / fragment pattern
```

JSON endpoints (§7) stay under `/api/...` for Slack, health scripts, agents, and external clients; the HTML routes above are for the browser.

The MTTR card shown on a resolved ticket:

```
INCIDENT RESOLVED  INC-0042
Detection        2 s
Diagnosis       19 s
Fix generated    8 s
Validation      31 s
Deployment      22 s
MTTR          1m 22s
```

---

## 5. Incident lifecycle

A ticket moves through these states. Each transition appends a `ticket_event`.

```
 detected ─▶ investigating ─▶ fix_proposed ─▶ sandbox_validated ─▶ reviewed ─▶ approved ─▶ deploying ─▶ resolved
     │             │                │                 │               │           │            │
     │             │                │                 │               │           └ DENIED ──▶ awaiting_human
     │             │                │                 └ not reproduced / fix failed ──▶ back to investigating
     │             │                └ Engineer                                      (max N attempts)
     │             └ Investigator (+ Guardian)
     └ health script / Slack / manual
                                                                                 post-deploy health worse ──▶ rolled_back
```

Step by step:

| # | State                 | Who                   | What happens                                                                 |
| - | --------------------- | --------------------- | ---------------------------------------------------------------------------- |
| 1 | `detected`            | Health script         | Check fails → ticket created with the `CheckResult` and metric deltas.       |
| 2 | `investigating`       | Investigator + Guardian | Diag investigates with read-only tools; Safety reviews the diagnosis.        |
| 3 | `fix_proposed`        | Engineer          | Sandbox spun up; incident reproduced; patch generated and applied in sandbox.|
| 4 | `sandbox_validated`   | Sandbox runner        | Load test + tests after the patch; compared against baseline.                |
| 5 | `reviewed`            | Guardian            | Risk score + proposed action produced.                                       |
| 6 | `approved` / `denied` | Command check gate    | Policy checks; backup taken on approval.                                     |
| 7 | `deploying`           | Run executor          | Staged rollout against production with monitoring.                           |
| 8 | `resolved`            | Health scripts        | Triggering check passes again; MTTR computed.                                |

A human can intervene at any point from the dashboard (approve a denied action, deny an approved one, or restore a backup).

---

## 6. Data model

```sql
tickets
  id            TEXT PK        -- INC-0042
  source        TEXT           -- health_check | slack | manual
  service       TEXT
  title         TEXT
  status        TEXT           -- see lifecycle
  severity      TEXT           -- low | medium | high | critical
  created_at    DATETIME
  resolved_at   DATETIME
  metrics_before JSON          -- baseline snapshot
  metrics_at_detect JSON       -- what tripped the check

ticket_events                  -- the timeline; everything the dashboard shows comes from here
  id            INTEGER PK
  ticket_id     TEXT FK
  ts            DATETIME
  actor         TEXT           -- health_check | investigator | engineer | guardian | gate | executor | human
  type          TEXT           -- detected | finding | diagnosis | patch | sandbox_result | review | gate_decision | run | resolved
  payload       JSON

deployments
  id            TEXT PK        -- DEP-0081
  ticket_id     TEXT FK
  action        TEXT           -- allowlisted action name
  args          JSON
  backup_ref    TEXT           -- git tag / snapshot path
  status        TEXT           -- running | succeeded | failed | rolled_back
  started_at    DATETIME
  finished_at   DATETIME
  evidence      JSON           -- sandbox run id, risk score, test results

health_checks
  name          TEXT PK
  service       TEXT
  enabled       BOOLEAN
  interval_s    INTEGER
  last_run      DATETIME
  last_ok       BOOLEAN
  last_result   JSON

sandbox_runs
  id            TEXT PK
  ticket_id     TEXT FK
  patch_ref     TEXT
  before        JSON           -- metrics with incident injected
  after         JSON           -- metrics with patch applied
  tests         JSON
  reproduced    BOOLEAN
  passed        BOOLEAN

settings
  key           TEXT PK
  value         JSON           -- thresholds, baselines, allowlist path, slack config
```

---

## 7. API reference

All components (frontend, Slack, health scripts, LLM agents, external tools) use this one API. Proposed v0:

| Method | Path                             | Used by            | Purpose                                             |
| ------ | -------------------------------- | ------------------ | --------------------------------------------------- |
| GET    | `/health`                        | everyone           | OCE's own liveness                                  |
| GET    | `/tickets`                       | frontend           | List tickets (filter by status)                     |
| POST   | `/tickets`                       | Slack, manual      | Create a ticket                                     |
| GET    | `/tickets/{id}`                  | frontend           | Ticket + full event timeline                        |
| POST   | `/tickets/{id}/events`           | LLM agents         | Append a finding / diagnosis / patch / review       |
| POST   | `/tickets/{id}/approve`          | human              | Approve a gated action                              |
| POST   | `/tickets/{id}/deny`             | human              | Deny a gated action                                 |
| POST   | `/actions`                       | Guardian         | Submit a proposed action → command check gate       |
| GET    | `/deployments`                   | frontend           | Past deployments with evidence                      |
| POST   | `/deployments/{id}/restore`      | frontend, executor | Restore from that deployment's backup               |
| GET    | `/checks`                        | frontend           | Registered health checks and last results           |
| POST   | `/checks/{name}/run`             | frontend           | Run a check now                                     |
| PUT    | `/checks/{name}`                 | frontend           | Enable/disable, change interval                     |
| GET    | `/logs`                          | frontend           | Ticket events + service logs                        |
| GET    | `/settings` / PUT `/settings`    | frontend           | Thresholds, baselines, Slack, allowlist             |
| GET    | `/stats/local-ai`                | frontend           | Cloud requests (0), bytes uploaded (0), model, hw   |

**Agent tool endpoints** (called by the LLM tool layer, scoped per agent):

| Method | Path                      | Available to          |
| ------ | ------------------------- | --------------------- |
| GET    | `/tools/logs`             | Diag                  |
| GET    | `/tools/metrics`          | Diag                  |
| GET    | `/tools/git`              | Diag, Solution        |
| GET    | `/tools/code`             | Diag, Solution        |
| POST   | `/tools/sandbox/patch`    | Solution              |
| POST   | `/tools/sandbox/test`     | Solution              |
| GET    | `/tools/diff`             | Safety                |
| GET    | `/tools/test-results`     | Safety                |

Auth: a simple bearer token per client type for the hackathon (frontend, agent-diag, agent-solution, agent-safety, health). Agent tokens only unlock their own tool endpoints — this is how permissions are enforced regardless of what the model asks for.

---

## 8. LLM scheduling and VRAM

**Current serving choice:** Ollama at `http://localhost:11434`; the Python client uses `POST /api/chat` with `model: "nemotron3:33b"`. Check the live model with `ollama ps`, and send a short test request before connecting any agents.

All three agent roles **share one loaded model** rather than running three copies. Tool calls and role-specific conversation contexts differentiate the agents. For the initial implementation, process agent requests sequentially; only introduce concurrent inference after measuring memory use, context sizes, response latency and Ollama behavior on the GX10.

The earlier design proposed two simultaneous request slots (Investigator + Guardian, then Engineer + Guardian). This remains an **optimization to benchmark**, not a verified memory constraint or existing scheduler. A bounded semaphore and request queue can be introduced in the orchestrator when justified by measurements.

Helpful verification commands on the GX10:

```bash
ollama list
ollama ps
curl http://localhost:11434/api/tags
ollama run nemotron3:33b "Reply with READY."
```

The model name `nemotron3:33b` should not be relabeled "Nemotron 3.5 Lightning" without confirming the actual model identity and event eligibility.

---

## 9. Fake company (`fakeco/`)

The infrastructure OCE monitors. A miniature company in Docker Compose that behaves enough like real internal infrastructure to demonstrate the concept.

```
CUSTOMER ─▶ FRONTEND ─▶ ORDERS API ─▶ POSTGRES
                              └─────▶ REDIS
```

| Service      | Notes                                                                                   |
| ------------ | --------------------------------------------------------------------------------------- |
| `orders-api` | FastAPI. Reads `DB_TIMEOUT` from config **committed in git** so the bad deploy is a real commit. Emits structured JSON logs, `/metrics` (prometheus_client), `/health`. Some queries `pg_sleep` ~150 ms so a 100 ms timeout actually fails. |
| `frontend`   | Optional thin web UI; can be skipped initially.                                         |
| `postgres`   | Official image (multi-arch).                                                            |
| `redis`      | Official image (multi-arch).                                                            |
| `prometheus` | Scrapes `orders-api`.                                                                   |
| `loadgen`    | Steady synthetic traffic. **Required** — without traffic there is no error rate to watch.|

Scripts:

- `inject_incident.sh` — commits `DB_TIMEOUT=3000 → 100` and redeploys. This is the demo trigger.
- `reset.sh` — returns fakeco to the healthy baseline commit.

Healthy baseline: error rate 0.2 %, P95 180 ms. Incident: error rate ~38 %, P95 ~2200 ms, HTTP 500s and DB timeouts rising.

---

## 10. Demo flow

1. Dashboard shows fakeco healthy. LOCAL AI panel: 0 cloud requests, 0 bytes uploaded.
2. Press **INJECT INCIDENT** (`inject_incident.sh`). Error rate jumps. **Nobody touches anything.**
3. Health script trips → `INC-0042` appears in Current Tickets within seconds.
4. Investigator findings stream into the ticket timeline: log investigation, metrics, deployment history → root cause with confidence and evidence.
5. Sandbox spins up, reproduces the incident (BEFORE metrics), Engineer applies the patch, AFTER metrics match baseline.
6. Guardian review: risk 8/100, proposed action `deploy_patch`.
7. Command gate checklist goes green; backup taken; run executor deploys in stages.
8. Health checks pass → **INCIDENT RESOLVED**, MTTR card.
9. **Optional closer:** disconnect the GX10 from the Internet mid-demo and re-run. Everything still works.

---

## 11. Repository layout

The `models/` tree below matches the current VS Code/SSH folder setup. Other directories and contents are shown as **planned** unless already implemented by the respective project owner.

```text
oce/
├── README.md
├── fakeco/                       # existing demo-company area; service content evolves
│   └── ...                       # production Docker stack, load generator, incident scripts
├── models/                       # current multi-agent Python package
│   ├── __init__.py
│   ├── config.py                  # model endpoint and runtime configuration
│   ├── requirements.txt
│   ├── core/
│   │   ├── __init__.py
│   │   ├── llm.py                 # Ollama API client
│   │   ├── agent.py               # shared agent loop
│   │   ├── tool_registry.py       # allowed tool schemas and dispatcher
│   │   └── state.py               # incident and agent context
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── investigator.py        # evidence-gathering agent
│   │   ├── engineer.py            # sandbox remediation agent
│   │   └── guardian.py            # independent safety reviewer
│   ├── prompts/
│   │   ├── investigator.txt
│   │   ├── engineer.txt
│   │   └── guardian.txt
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── docker_tools.py        # logs and container inspection
│   │   ├── filesystem_tools.py    # guarded code/config reads
│   │   ├── prometheus_tools.py    # metrics queries
│   │   └── sandbox_tools.py       # sandbox patch/test operations
│   ├── orchestrator.py            # incident handoffs and retries
│   └── main.py                    # entry point for agents
├── oce/                           # planned Main Process/API implementation
│   ├── main.py                    # FastAPI and scheduling
│   ├── models.py                  # API/data contracts (distinct from top-level models/)
│   ├── db.py
│   ├── api/
│   ├── web/
│   ├── templates/
│   ├── static/
│   ├── checks/
│   ├── sandbox.py
│   ├── gate.py
│   ├── allowlist.yaml
│   ├── backup.py
│   ├── executor.py
│   └── slack.py
└── docs/                         # planned supporting documentation
```

**Running the Python package (once implemented):** from the repository root, use `python3 -m models.main` so `models.core` and `models.agents` imports resolve consistently. A successful Ollama API check does not, by itself, establish that these Python modules are implemented.

---

## 12. Tech stack

| Layer          | Choice                                   | Why                                                   |
| -------------- | ---------------------------------------- | ----------------------------------------------------- |
| Hardware       | ASUS Ascent GX10 (GB10, ARM64, 128 GB)   | Competition platform; everything must be ARM-compatible|
| Model          | `nemotron3:33b` (initial, local)         | One shared model across three agent roles              |
| Model serving  | Ollama, local `/api/chat` endpoint       | Confirmed installed; inference still needs validation |
| Backend        | Python 3.12, FastAPI, SQLite             | Fast to build, async, ARM-friendly                     |
| Infra target   | Docker Compose (multi-arch images)       | fakeco + sandbox                                       |
| Metrics        | Prometheus + prometheus_client           | Standard, lightweight                                  |
| Logs           | Structured JSON to stdout / `docker logs`| Skip Loki unless time allows                           |
| Dashboard      | HTMX + Jinja2 + Alpine.js, Chart.js      | Server-rendered by FastAPI; no Node, no build step, works offline (see §4.10) |
| Auth           | Local accounts, session cookie           | Must work offline; no external IdP                     |
| Load gen       | Small Python script or Locust            |                                                        |

---

## 13. Work split and roadmap

**AI subsystem (`models/`):** Own the shared Ollama client, bounded agent loop, role prompts, guarded tool integrations, agent state, three agents, and orchestration.

**Platform (`oce/`, `fakeco/`):** Own the API, database, health checks, policy gate, backups, executor, isolated sandbox, dashboard and optional Slack integration. Agree on common event, evidence and tool contracts early.

| Stage | Deliverable | Status |
| --- | --- | --- |
| **A0** | Create `models/` structure and discover installed Ollama models | Folder setup created; model tags confirmed |
| **A1** | Test `nemotron3:33b` inference, implement `config.py` and `core/llm.py` | Next |
| **A2** | Implement tool registry and bounded tool-calling agent loop | Planned |
| **A3** | Investigator: Docker logs, container state, metrics and read-only diagnosis | Planned |
| **A4** | Engineer: sandbox-scoped patching, reproduction and test iteration | Planned |
| **A5** | Guardian: evidence review, risk evaluation and advisory decision | Planned |
| **A6** | Orchestrator: structured handoffs, incident state and audit trail | Planned |
| **A7** | Integrate with fakeco, API, gate, backup, executor and dashboard | Planned |
| **A8** | End-to-end incident demo, latency profiling, offline and safety testing | Planned |

**Early integration contracts:** Agree on structured Investigator diagnosis, Engineer patch/test evidence, Guardian review, and command gate action request formats. Do not allow arbitrary shell commands or production filesystem writes through agent tools.

---

## 14. Open questions

- Does the competition specifically require Nemotron 3.5 Lightning, and does any installed Ollama tag satisfy that requirement?
- How many concurrent model requests can Ollama serve reliably on this GX10 at the desired context length?
- What concrete API/data schemas will connect the `models/` orchestrator to the Main Process and incident database?
- How will sandbox isolation be enforced at the container, network, mount and credential levels?
- How will Guardian's advisory recommendation translate to a deterministic gate decision and human approval?
- Real traffic-split canary or demo-only visual stages?
- Slack integration in the initial demo or as a stretch goal?
- Log source: `docker logs`, shared log file, or a dedicated log system?
