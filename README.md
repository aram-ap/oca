# OCE — On-Call Engineer

**A fully local autonomous SRE team.** OCE monitors a company's private infrastructure, detects incidents, diagnoses the root cause, develops a fix, proves it in an isolated sandbox, and deploys it under deterministic policy control — without source code, logs, credentials, or infrastructure data ever leaving the machine.

Everything runs on a single ASUS Ascent GX10 (NVIDIA GB10, 128 GB unified memory) using a locally served NVIDIA Nemotron model.

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
5. **One model, many agents.** A single locally served Nemotron model plays every agent role. An agent is: the same model + a different system prompt + different context + different tools + different permissions.
6. **The API is the product.** The frontend portal is just one client. Health scripts, Slack, the LLM agents, and any external tools all talk to the same Main Process API, so local and non-local interfaces are both supported.

---

## 3. System architecture

```
                              ASUS ASCENT GX10  (everything below runs on this box)
 ┌──────────────────────────────────────────────────────────────────────────────────────┐
 │                                                                                      │
 │   ┌──────────────┐     ┌─────────────┐     ┌──────────────────────────────────┐      │
 │   │ Frontend     │     │ Slack API   │     │ Health status scripts            │      │
 │   │ portal       │     │ (tickets)   │     │  • network endpoint checks       │      │
 │   └──────┬───────┘     └──────┬──────┘     │  • log error-parse checks        │      │
 │          │                    │            │  • Slack ticket reading          │      │
 │   ┌──────┴───────┐            │            └──────────────┬───────────────────┘      │
 │   │ API endpoint │            │                           │                          │
 │   │ (external    │            │                           │                          │
 │   │  clients)    │            │                           │                          │
 │   └──────┬───────┘            │                           │                          │
 │          │                    ▼                           ▼                          │
 │          │           ┌──────────────────────────────────────────┐    ┌───────────┐   │
 │          └──────────▶│           MAIN PROCESS / API             │◀──▶│ Database  │   │
 │                      │  tickets · orchestration · LLM scheduler │    │ (SQLite)  │   │
 │                      └───────┬────────────────┬────────────────┘    └───────────┘   │
 │                              │                │                                      │
 │                              ▼                ▼                                      │
 │                      ┌──────────────┐   ┌─────────────────────────┐                  │
 │                      │  Diag LLM    │   │  SANDBOX                │                  │
 │                      │ (read-only)  │   │  ┌───────────────────┐  │                  │
 │                      └──────┬───────┘   │  │ Solution LLM      │  │                  │
 │                             │           │  │ (edits sandbox    │  │                  │
 │                             ▼           │  │  copy of prod)    │  │                  │
 │                      ┌──────────────┐   │  └───────────────────┘  │                  │
 │                      │  Safety LLM  │◀──┤  before/after metrics   │                  │
 │                      │ (reviews)    │   └─────────────────────────┘                  │
 │                      └──────┬───────┘                                                │
 │                             │ proposed action                                        │
 │                             ▼                                                        │
 │                      ┌──────────────┐    ┌──────────────┐                            │
 │                      │ Command      │───▶│ Backup       │                            │
 │                      │ check gate   │    │ system       │                            │
 │                      │ (allowlist)  │    └──────────────┘                            │
 │                      └──────┬───────┘                                                │
 │                             │ approved                                               │
 │                             ▼                                                        │
 │                      ┌──────────────┐    ┌────────────────────────────────────┐      │
 │                      │ Run          │───▶│ "PRODUCTION"  (fakeco Docker stack)│      │
 │                      │ executor     │    │  frontend · orders-api · pg · redis│      │
 │                      └──────────────┘    └────────────────────────────────────┘      │
 │                                                                                      │
 │   ┌──────────────────────────────────────────────────────────────────────────────┐   │
 │   │ LOCAL NEMOTRON SERVER — one model, shared by Diag / Solution / Safety        │   │
 │   └──────────────────────────────────────────────────────────────────────────────┘   │
 └──────────────────────────────────────────────────────────────────────────────────────┘
```

The **Main Process** is the hub. Every other component is either an input to it (health scripts, Slack, frontend, external API), a worker it orchestrates (the three LLM agents, the sandbox), or an effector it drives (gate → backup → run).

---

## 4. Components

### 4.1 Main Process / API

The central service. Owns the database, exposes the REST API, runs the health-check scheduler, orchestrates the incident pipeline, and schedules LLM work.

Responsibilities:

- Serve the REST API (see [§7](#7-api-reference)) to the frontend, Slack, health scripts, and external clients.
- Run registered health checks on a timer and open tickets when they fail.
- Drive each ticket through the [incident lifecycle](#5-incident-lifecycle).
- Enforce the two-slot LLM scheduler (see [§8](#8-llm-scheduling-and-vram)).
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

All three agents are the **same local Nemotron model** with different system prompts, context, tools, and permissions. Tools are Python functions exposed to the model via the Main Process; the model can only call what it is given.

| Agent            | Role                                                                                       | Tools (allowed)                                                                 | Forbidden                                    |
| ---------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------- | -------------------------------------------- |
| **Diag LLM**     | Investigates the ticket: reads logs, metrics, git history, code. Produces a root-cause report with confidence and evidence. | `query_logs()` `query_metrics()` `inspect_git()` `read_code()` | edit anything, deploy, run commands          |
| **Solution LLM** | Receives diagnosis + code + diff. Generates a candidate fix and applies it **inside the sandbox only**. Runs the load test and reports before/after metrics. | `read_code()` `generate_patch()` `modify_sandbox()` `run_sandbox_test()` | touch production, deploy                     |
| **Safety LLM**   | Reviews what Diag and Solution produced. Evaluates the diff, test results, and blast radius. Produces a risk score and a *proposed action* for the gate. | `inspect_diff()` `inspect_test_results()` `evaluate_risk()` `propose_action()` | modify the patch, bypass the gate, run anything |

Diag output (example):

```
ROOT CAUSE
Deployment #81 reduced database timeout from 3000ms to 100ms.
Confidence: 96%
Evidence:
  ✓ Incident began 7 min after deployment
  ✓ 92% of failures contain "database request timeout"
  ✓ DB itself healthy; no CPU/memory saturation
```

Safety output (example):

```
RELEASE REVIEW
Files changed 1   Lines changed 1
DB migration NO   Auth affected NO   Public API affected NO   Deps changed NO
Sandbox: incident reproduced ✓  baseline restored ✓
Risk score: 8/100  LOW

PROPOSED ACTION
  deploy_patch(incident="INC-0042", patch="FIX-001")
```

The Safety LLM's output is a *request*, not a command. It goes to the gate.

### 4.5 Sandbox

An isolated copy of production where the Solution LLM works and where fixes are proven.

Mechanism:

1. Create a git worktree of the `fakeco/` repo at the currently deployed commit.
2. Start a second Docker Compose project (`docker compose -p sandbox`) from that worktree on separate ports / network.
3. Inject the same failure that production is experiencing (replay the bad config/commit).
4. Run the load generator → record **BEFORE** metrics. The incident must reproduce, or the diagnosis is sent back to Diag.
5. Solution LLM applies its patch via `modify_sandbox()`.
6. Run the load generator again → record **AFTER** metrics. Run the test suite.
7. Compare AFTER against the stored healthy baseline. All of this is attached to the ticket as evidence.

```
                BASELINE      BEFORE FIX     AFTER FIX
Error rate       0.20%         38.12%         0.17%  ✓
P95              180 ms        2188 ms        186 ms ✓
Tests            PASS          —              PASS   ✓
```

The sandbox is torn down after the ticket closes. The Solution LLM has no network path to the production stack.

### 4.6 Command check gate

The single choke point between AI and the real system. **Deterministic, no LLM.**

Every action request has the shape:

```json
{
  "action": "deploy_patch",
  "args": { "incident": "INC-0042", "patch": "FIX-001" },
  "requested_by": "safety_llm",
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
     │             │                └ Solution LLM                                      (max N attempts)
     │             └ Diag LLM (+ Safety LLM)
     └ health script / Slack / manual
                                                                                 post-deploy health worse ──▶ rolled_back
```

Step by step:

| # | State                 | Who                   | What happens                                                                 |
| - | --------------------- | --------------------- | ---------------------------------------------------------------------------- |
| 1 | `detected`            | Health script         | Check fails → ticket created with the `CheckResult` and metric deltas.       |
| 2 | `investigating`       | Diag LLM + Safety LLM | Diag investigates with read-only tools; Safety reviews the diagnosis.        |
| 3 | `fix_proposed`        | Solution LLM          | Sandbox spun up; incident reproduced; patch generated and applied in sandbox.|
| 4 | `sandbox_validated`   | Sandbox runner        | Load test + tests after the patch; compared against baseline.                |
| 5 | `reviewed`            | Safety LLM            | Risk score + proposed action produced.                                       |
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
  actor         TEXT           -- health_check | diag_llm | solution_llm | safety_llm | gate | executor | human
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
| POST   | `/actions`                       | Safety LLM         | Submit a proposed action → command check gate       |
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

The GX10 has limited VRAM/processing for concurrent inference. Constraint from the design: **at most 2 LLM processes at once.** The pipeline is built around two pairs:

```
Phase A:  Safety LLM  +  Diag LLM        (investigate, review diagnosis)
Phase B:  Safety LLM  +  Solution LLM    (fix in sandbox, review fix)
```

The Main Process implements this as a two-slot semaphore around LLM work. The Safety LLM holds one slot across both phases; the second slot is handed from Diag to Solution.

> **Note:** if all three agents are one Nemotron model served by a single server (vLLM / NIM), the model is loaded **once** and the limit is on concurrent *requests*, not loaded models. In that case the semaphore is about request concurrency and context size, and the pairing above is a scheduling preference rather than a hard VRAM limit. Confirm with whoever owns model serving.

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
4. Diag LLM findings stream into the ticket timeline: log investigation, metrics, deployment history → root cause with confidence and evidence.
5. Sandbox spins up, reproduces the incident (BEFORE metrics), Solution LLM applies the patch, AFTER metrics match baseline.
6. Safety LLM review: risk 8/100, proposed action `deploy_patch`.
7. Command gate checklist goes green; backup taken; run executor deploys in stages.
8. Health checks pass → **INCIDENT RESOLVED**, MTTR card.
9. **Optional closer:** disconnect the GX10 from the Internet mid-demo and re-run. Everything still works.

---

## 11. Repository layout

```
oce/
├── README.md
├── fakeco/                 # the fake company being monitored
│   ├── docker-compose.yml
│   ├── orders-api/
│   ├── frontend/
│   ├── loadgen/
│   ├── prometheus/
│   ├── inject_incident.sh
│   └── reset.sh
├── oce/                    # the on-call engineer
│   ├── main.py             # FastAPI app, scheduler, orchestrator
│   ├── models.py           # Ticket, TicketEvent, Deployment, ActionRequest, CheckResult …
│   ├── db.py               # SQLite access layer
│   ├── api/                # route modules (tickets, deployments, checks, actions, tools, settings)
│   ├── checks/             # health check plug-ins
│   ├── agents/             # Diag / Solution / Safety prompts + tool bindings (AI team)
│   ├── llm.py              # Nemotron client + 2-slot scheduler
│   ├── sandbox.py          # worktree + compose -p sandbox + load test
│   ├── gate.py             # command check gate (policy engine)
│   ├── allowlist.yaml      # named, parameterised actions
│   ├── backup.py
│   ├── executor.py         # run executor + staged rollout + auto-restore
│   └── slack.py
├── dashboard/              # frontend portal
└── docs/
```

---

## 12. Tech stack

| Layer          | Choice                                   | Why                                                   |
| -------------- | ---------------------------------------- | ----------------------------------------------------- |
| Hardware       | ASUS Ascent GX10 (GB10, ARM64, 128 GB)   | Competition platform; everything must be ARM-compatible|
| Model          | NVIDIA Nemotron (local)                  | Preloaded on the box; single shared model              |
| Model serving  | vLLM / NIM, OpenAI-compatible endpoint   | TBD by AI team                                         |
| Backend        | Python 3.12, FastAPI, SQLite             | Fast to build, async, ARM-friendly                     |
| Infra target   | Docker Compose (multi-arch images)       | fakeco + sandbox                                       |
| Metrics        | Prometheus + prometheus_client           | Standard, lightweight                                  |
| Logs           | Structured JSON to stdout / `docker logs`| Skip Loki unless time allows                           |
| Dashboard      | TBD (React/Vite or server-rendered)      | Consumes the REST API only                             |
| Load gen       | Small Python script or Locust            |                                                        |

---

## 13. Work split and roadmap

**Non-AI system (this track):** Main Process/API, DB, health scripts, gate, backup, executor, sandbox runner, Slack, fakeco.
**AI (teammates):** Nemotron serving, agent prompts, tool bindings, agent outputs.

Build order — each milestone is demoable on its own:

| Milestone | Deliverable                                                                                 | Unblocks            |
| --------- | ------------------------------------------------------------------------------------------- | ------------------- |
| **M0**    | `models.py` contracts + stub API returning canned data                                       | AI team, dashboard  |
| **M1**    | `fakeco/` up with loadgen; `inject_incident.sh` visibly raises error rate in Prometheus       | everything          |
| **M2**    | Health checks + ticket creation. Inject → ticket appears.                                    | Diag agent          |
| **M3**    | Gate + allowlist + backup + executor. Manual `POST /actions` restores the timeout.            | Safety agent        |
| **M4**    | Sandbox runner with before/after metrics.                                                    | Solution agent      |
| **M5**    | Dashboard pages against the real API; Slack.                                                 | demo                |
| **M6**    | End-to-end run with all three agents; MTTR card; offline demo.                               | judging             |

Early checks:

- Test on the GX10 early — all images must be ARM64.
- Agree on `ActionRequest` and the agent tool signatures in M0 before the AI team writes prompts.

---

## 14. Open questions

- Is Nemotron served as one model (concurrency limit) or loaded per agent (VRAM limit)? Affects [§8](#8-llm-scheduling-and-vram).
- Real traffic-split canary (nginx weights) or visual stages only?
- Does the demo include Slack, or is it stretch?
- Log source: `docker logs` scraping vs. a shared log file vs. Loki.
- Human-in-the-loop: fully autonomous for the demo, or require a dashboard approval click before deploy?
