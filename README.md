# OCA — On-Call Agent

**A fully local autonomous SRE team (in development).** OCA monitors a company's private infrastructure, detects incidents, diagnoses the root cause, develops a fix, proves it in an isolated sandbox, and deploys it under deterministic policy control — without source code, logs, credentials, or infrastructure data ever leaving the machine.

Everything runs on a single ASUS Ascent GX10 (NVIDIA GB10, 128 GB unified memory) using Ollama to serve one shared local Nemotron model to three specialized agents.

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
   - [Ticketing integrations (Jira, Slack)](#49-ticketing-integrations-jira-slack)
   - [Frontend portal](#410-frontend-portal)
5. [Incident lifecycle](#5-incident-lifecycle)
6. [Data model](#6-data-model)
7. [API reference](#7-api-reference)
8. [LLM serving and scheduling](#8-llm-serving-and-scheduling)
9. [Fake company (`fakeco/`)](#9-fake-company-fakeco)
10. [Demo flow](#10-demo-flow)
11. [Repository layout](#11-repository-layout)
12. [Tech stack](#12-tech-stack)
13. [Work split and roadmap](#13-work-split-and-roadmap)
14. [Decisions](#14-decisions)
15. [Open questions](#15-open-questions)

---

## 1. Why local

When production goes down, engineers routinely paste some of their company's most sensitive data — source code, logs, config, architecture, security findings — into cloud AI services to get help. OCA removes that dependency entirely.

| Reason      | What it means for OCA                                                                 |
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
Model                         NVIDIA Nemotron (via Ollama)
Inference                     100% LOCAL
Cloud LLM requests            0
Infrastructure data uploaded  0 bytes
Ticketing sync (Jira)         self-hosted · 0 bytes external     ← or "metadata only · 1.2 KB" if Jira Cloud
```

The last line exists so the panel stays honest when an external ticketing system is connected: ticket IDs and status go out, infrastructure data never does (see [§4.9](#49-ticketing-integrations-jira-slack)).

---

## 2. Design principles

1. **Software detects, AI reasons.** Deterministic health scripts decide *that* something is wrong. The LLM decides *why* and *what to do about it*. No LLM stares at metrics.
2. **AI decides what should happen. Deterministic policy decides what AI is allowed to make happen.** The LLM never receives unrestricted shell access. Every action passes through the command check gate, which only runs pre-configured, allowlisted commands.
3. **Prove it before you ship it.** A candidate fix must reproduce the incident and then resolve it in an isolated sandbox before it is eligible for deployment.
4. **Always have a way back.** A backup is taken before every change, and every deployment can be restored from the dashboard.
5. **One model, three real agents.** Investigator, Engineer, and Guardian share a single locally served Nemotron model, but each has its own system prompt, context, tool permissions, and bounded observe–act agent loop. A prompt alone is not an agent.
6. **The API is the product.** The frontend portal is just one client. Health scripts, Jira, Slack, the LLM agents, and any external tools all talk to the same Main Process API, so local and non-local interfaces are both supported.
7. **External ticketing is a mirror, not a source of truth.** Jira/Slack can open tickets and receive status, but the local DB owns all state and all evidence. If the ticketing system is unreachable, the pipeline continues and syncs later.

---

## 3. System architecture

This is the **target architecture**. The agent code lives in the top-level `models/` Python package (structure created, implementation in progress); the platform code lives in `oce/` (planned); the company-under-test lives in `fakeco/`.

```text
                    ASUS ASCENT GX10 (GB10 · 128 GB unified memory)
┌────────────────────────────────────────────────────────────────────────┐
│ Browser dashboard / health checks / Jira · Slack / external clients    │
│                              │                                         │
│                     oce/  Main Process / API  (FastAPI)                │
│          Incident state (SQLite) · scheduler · gate · sync outbox      │
│                              │                                         │
│                    models/orchestrator.py  (background task)           │
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
│                  models/tools/*  → HTTP → oce /api/tools/*              │
│                              │                                         │
│               models/core/llm.py → Ollama :11434                       │
│                     nemotron3:33b (initial)                            │
│                                                                        │
│  Observability       fakeco production       Isolated sandbox         │
│  Docker / Prometheus     Docker Compose      Docker Compose clone     │
│                                                                        │
│  Guardian review → orchestrator → command gate → (human) → backup      │
│                                   → executor → production              │
└────────────────────────────────────────────────────────────────────────┘
```

**Control boundaries:** An LLM chooses among registered tools; Python validates arguments, enforces per-agent tool allowlists, and records the results. Investigator cannot write. Engineer can write only to a sandbox worktree. Guardian can review but cannot deploy. The deterministic command gate and executor — not the LLM — control production changes, and by default a human approves each deployment from the dashboard (see [§14](#14-decisions)).

**Local model inventory observed on the GX10** (`ollama list`):

| Ollama model              | Approx. disk size | Role                                        |
| ------------------------- | ----------------: | ------------------------------------------- |
| `nemotron3:33b`           |             27 GB | Initial shared model for all three agents   |
| `nemotron-3-super:latest` |             86 GB | Larger Nemotron candidate to benchmark later |
| `qwen3.6:35b`             |             23 GB | Alternative model for evaluation            |
| `gemma4:26b`              |             17 GB | Alternative model for evaluation            |

Installed ≠ validated: inference and tool-calling with `nemotron3:33b` still need to be tested (roadmap A1).

---

## 4. Components

### 4.1 Main Process / API

The central service. Owns the database, exposes the REST API, serves the dashboard, runs the health-check scheduler, orchestrates the incident pipeline, and hosts the command gate.

Responsibilities:

- Serve the JSON API under `/api` (see [§7](#7-api-reference)) to Slack, health scripts, agents, and external clients, and the HTML dashboard at `/` (see [§4.10](#410-frontend-portal)).
- Run registered health checks on a timer and open tickets when they fail.
- Drive each ticket through the [incident lifecycle](#5-incident-lifecycle) by launching `models.orchestrator` as a background task per ticket.
- Route every proposed action to the command check gate; never execute anything directly.
- Append every state change to the ticket's event timeline so the dashboard and logs are always derivable from the DB.
- Mirror ticket state to external ticketing systems (Jira, Slack) through a retrying outbox, so a ticketing outage never blocks the pipeline.

Implementation: Python, FastAPI, async. **Single process.** The orchestrator is imported and run in-process as a background task; agents reach their tools over HTTP to `localhost` so the same tool code also works when the agent package is run standalone (`python3 -m models.main`) for development.

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
    detail: str               # human-readable summary
    metrics: dict[str, float] # e.g. {"error_rate": 0.387, "p95_ms": 2240}
```

Initial checks:

| Check                 | What it does                                                                        | Opens a ticket when                    |
| --------------------- | ----------------------------------------------------------------------------------- | -------------------------------------- |
| `endpoint_check`      | HTTP GET on each service's `/health`                                                | non-2xx or timeout                     |
| `log_error_parse`     | Tails service logs, computes error rate over a sliding window                       | error rate > threshold                 |
| `metrics_check`       | Queries the Prometheus HTTP API (`/api/v1/query`) for error rate and P95 latency    | value exceeds stored baseline × factor |
| `jira_ticket_reader`  | Polls Jira with a JQL filter for new incident issues (see §4.9)                     | a new matching issue appears           |
| `slack_ticket_reader` | Polls a Slack channel for incident reports                                          | a new report message appears           |

The Main Process stores the result of every run in `health_checks` (latest state) and opens a ticket with the triggering `CheckResult` attached as the first event. Thresholds and baselines live in `settings`.

### 4.4 LLM agents

The AI implementation is contained in `models/` and comprises **three separate tool-using agents**, not three prompt templates. Each loads a role-specific prompt from `models/prompts/`, is granted a subset of the tools in `models/tools/`, and runs the shared bounded loop in `models/core/agent.py`: decide → call a tool → observe → continue or return.

| Agent            | Source + prompt                                  | Responsibilities                                                                                          | Tool access                                                         | Forbidden                                            |
| ---------------- | ------------------------------------------------ | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- | ---------------------------------------------------- |
| **Investigator** | `agents/investigator.py` + `prompts/investigator.txt` | Examine logs, Prometheus metrics, container state, git history, and relevant code; report suspected root cause with evidence | Read-only: `docker_tools`, `prometheus_tools`, `filesystem_tools`, `git_tools` | Editing files, commands with side effects, deployment |
| **Engineer**     | `agents/engineer.py` + `prompts/engineer.txt`    | Create candidate patches, apply them to the sandbox copy, run load test and tests, iterate               | Read access to code; **sandbox-scoped** `sandbox_tools`             | Editing production, deployments, unrestricted shell  |
| **Guardian**     | `agents/guardian.py` + `prompts/guardian.txt`    | Inspect proposed diff, test evidence, operational/security risk, and policy constraints; recommend a decision | Read-only: `review_tools` (diff, test results, risk evaluation)      | Modifying patches, calling the gate, deploying       |

**Tool architecture (decision D1):** every `models/tools/*.py` function is a thin client for a Main Process endpoint under `/api/tools/...`. Permissions are enforced twice:

1. `tool_registry.py` decides which tools an agent is *offered* (the model cannot name a tool it was not given).
2. Each agent runs with its own API token; the Main Process only serves the tool endpoints that token is scoped to ([§7](#7-api-reference)). Even a confused or manipulated agent cannot reach a tool outside its role.

**Shared implementation:**

- `models/config.py` — model name, Ollama URL, Main Process URL, agent tokens, timeouts.
- `models/core/llm.py` — client for Ollama's local `/api/chat` endpoint.
- `models/core/agent.py` — bounded tool-call → execution → observation loop, with step limits, timeouts, and error handling.
- `models/core/tool_registry.py` — typed tool schemas, argument validation, per-agent allowlists, dispatch.
- `models/core/state.py` — incident context, observations, and evidence exchanged between steps.
- `models/orchestrator.py` — Investigator → Engineer → Guardian handoffs, retries, escalation, and submission of the final `ActionRequest` to the gate (decision D2).
- `models/main.py` — standalone entry point for running the agents against a ticket during development.

A **shared model does not mean shared permissions**: each agent has its own prompt, execution history, and allowed tools. Tool outputs such as logs and repository contents are untrusted data and must not be treated as instructions.

**Agent output contracts:**

- **Investigator:** structured diagnosis, evidence references, confidence, unresolved questions.
- **Engineer:** proposed patch/diff, sandbox reproduction evidence, test commands, before/after results.
- **Guardian:** risk score (0–100), policy concerns, decision (`APPROVE`, `REJECT`, `NEEDS_TESTING`), justification. `APPROVE` is **advisory**; the orchestrator turns it into an `ActionRequest`, and only the gate (and, by default, a human) can approve execution.

Example Investigator output:

```
ROOT CAUSE
Deployment #81 reduced database timeout from 3000ms to 100ms.
Confidence: 96%
Evidence:
  ✓ Incident began 7 min after deployment
  ✓ 92% of failures contain "database request timeout"
  ✓ DB itself healthy; no CPU/memory saturation
```

Example Guardian output:

```
RELEASE REVIEW
Files changed 1   Lines changed 1
DB migration NO   Auth affected NO   Public API affected NO   Deps changed NO
Sandbox: incident reproduced ✓  baseline restored ✓
Risk score: 8/100  LOW
Decision: APPROVE
```

### 4.5 Sandbox

An isolated copy of production where the Engineer works and where fixes are proven. Managed by `oce/sandbox.py`; the Engineer reaches it only through `sandbox_tools` → `/api/tools/sandbox/*`.

Mechanism:

1. Create a git worktree of the `fakeco/` repo at the currently deployed commit.
2. Start a second Docker Compose project (`docker compose -p sandbox`) from that worktree on a separate network and ports.
3. Inject the same failure production is experiencing (replay the bad config/commit).
4. Run the load generator → record **BEFORE** metrics. The incident must reproduce, or the ticket goes back to the Investigator.
5. Engineer applies its patch via `sandbox_tools.apply_patch()`.
6. Run the load generator again → record **AFTER** metrics. Run the test suite.
7. Compare AFTER against the stored healthy baseline. Everything is attached to the ticket as a `sandbox_result` event and a `sandbox_runs` row.

```
                BASELINE      BEFORE FIX     AFTER FIX
Error rate       0.20%         38.12%         0.17%  ✓
P95              180 ms        2188 ms        186 ms ✓
Tests            PASS          —              PASS   ✓
```

The sandbox is torn down after the ticket closes. The sandbox network has no route to the production stack, and sandbox tools refuse any path outside the worktree.

### 4.6 Command check gate

The single choke point between AI and the real system. **Deterministic, no LLM.** Lives in `oce/gate.py`.

Every action request has the shape:

```json
{
  "action": "deploy_patch",
  "args": { "incident": "INC-0042", "patch": "FIX-001" },
  "requested_by": "orchestrator",
  "evidence": { "sandbox_run_id": "...", "risk_score": 8, "guardian_decision": "APPROVE" }
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
│ 6. Guardian decision == APPROVE?      ✓  │
│ 7. Risk score below threshold?        ✓  │
│ 8. Not on the blocklist / cooldown?   ✓  │
└──────────────────────────────────────────┘
   → POLICY PASS → if require_human_approval: awaiting_approval (dashboard button)
                   else: approved
   → APPROVED    → backup → run executor
   → DENIED      → ticket event + dashboard, waits for human
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
    requires: [backup, sandbox_pass, tests_pass, guardian_approve, risk_below: 30]
  rollback:
    args: { deployment_id: { type: str } }
    requires: [backup]
```

Everything the gate does — passed, denied, awaiting, approved — is written to `ticket_events`, and approvals to `deployments`.

### 4.7 Backup system

Called by the gate after approval, before the executor runs. For the hackathon:

- Tag the current `fakeco/` commit (`backup/<deployment_id>`).
- Snapshot the running config (env, compose state) to `backups/<deployment_id>/`.
- Record the backup reference in `deployments`.

**Restore** = check out the tag, re-apply the snapshot, redeploy. Exposed as `POST /api/deployments/{id}/restore` and as the **Restore Backup** button on the Past Deployments page. Also triggered automatically if post-deploy health checks get worse.

### 4.8 Run executor

Executes an approved action against the production stack. It knows how to perform each allowlisted action and nothing else. Lives in `oce/executor.py`.

Deployment is staged and monitored:

```
snapshot → canary → monitor → widen → monitor → 100% → re-run health checks
```

For the hackathon, "canary percentages" are visual stages; real traffic splitting can be added with nginx upstream weights if time allows. After the run completes, the health checks re-run. If the triggering check passes, the ticket is resolved; if things are worse, the executor calls Restore and the ticket moves to `rolled_back`.

### 4.9 Ticketing integrations (Jira, Slack)

OCA connects to the ticketing systems a company already uses, so an incident can enter from a support ticket and the people watching that ticket see it progress and close. All integrations share one shape, implemented in `oce/integrations/`:

```python
class TicketingIntegration:
    name: str                                   # "jira", "slack"

    # inbound: external ticket → local ticket
    def poll(self) -> list[InboundTicket]: ...  # run on a timer by the *_ticket_reader check
    def parse_webhook(self, body) -> InboundTicket | None: ...

    # outbound: local ticket state → external system
    def on_event(self, ticket, event) -> OutboundMessage | None: ...
```

Inbound creates a ticket with `source: jira|slack` and an `external_ref`. Outbound messages are written to the `integration_outbox` table and delivered by a background worker with retries. **The pipeline never waits on an external system.**

#### Jira

Jira is the primary integration. Supports **Jira Data Center / Server** (self-hosted, fully local) and **Jira Cloud** (external SaaS). Which one is connected changes what OCA is allowed to send.

**Inbound — Jira issue → OCA ticket**

| Step | Detail                                                                                                                                                   |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Poll | `jira_ticket_reader` runs `settings.jira.jql` every `poll_interval_s` (default 30 s). Example: `project = OPS AND status = Open AND labels = oca`        |
| Webhook (optional) | `POST /api/integrations/jira/webhook` for Data Center or a reachable Cloud setup. Polling is the default because it needs no inbound exposure.  |
| Map  | `summary` → `title`; `priority` → `severity` (Highest/High → high, Medium → medium, Low/Lowest → low); `components[0]` or custom field → `service`; `description` → first `ticket_event` (`type: reported`) |
| Dedupe | `external_ref.key` is unique per ticket; re-polling the same issue is a no-op.                                                                         |
| Link | OCA posts a comment on the Jira issue: `OCA is investigating. Dashboard: http://<gx10>/tickets/INC-0043` and sets the issue to **In Progress**.         |

A Jira-sourced ticket has no `metrics_at_detect`; the Investigator starts from the description and gathers its own evidence by running the health checks and querying metrics on demand.

**Outbound — OCA ticket → Jira issue**

| Local event            | Jira action                                                                                      |
| ---------------------- | ------------------------------------------------------------------------------------------------ |
| `detected` (local source, if `jira.create_on_detect`) | Create issue in `jira.project`, label `oca`, link back to dashboard; store `external_ref`. |
| `diagnosis`            | Comment: one-line root cause + confidence (content per outbound policy below)                    |
| `awaiting_approval`    | Comment: "Fix validated in sandbox, risk N/100. Awaiting approval on the OCA dashboard: <link>" |
| `deploying`            | Comment + transition to **In Review** (configurable)                                             |
| `resolved`             | Comment with MTTR card + transition to **Done**                                                  |
| `rolled_back` / `denied` | Comment + transition back to **Open**, assign to on-call (configurable)                        |

Transition names are mapped in `settings.jira.status_map` because every Jira workflow is different. Approve/deny stays on the OCA dashboard (authenticated, local); a Jira comment is never an instruction to OCA.

**Outbound data policy (decision D8)** — the part that keeps the local-AI claim true:

| `jira.outbound_detail` | What OCA sends                                                                 | Default for         |
| ---------------------- | ------------------------------------------------------------------------------ | ------------------- |
| `minimal`              | Ticket ID, status transitions, dashboard link, MTTR numbers. **No free text from agents.** | Jira Cloud          |
| `summary`              | `minimal` + the Investigator's root-cause one-liner and the Guardian's risk score | Jira Data Center    |
| `off`                  | Inbound only; nothing written to Jira                                          | —                   |

Never sent at any level: logs, code, diffs, metrics series, container names beyond the `service` label, sandbox output. Outbound comments are built from templates over whitelisted structured fields, never from raw agent text. Bytes sent to Jira Cloud are counted and shown on the LOCAL AI panel.

**Auth:** Jira personal access token (Data Center) or API token + email (Cloud), stored in `settings.jira` on the box. Client is `httpx` against Jira REST API v2 — no SDK needed.

#### Slack

Same interface, secondary priority:

- **Inbound:** `slack_ticket_reader` polls a channel; a matching message creates a ticket (`source: slack`).
- **Outbound:** state-change messages to a channel, same `outbound_detail` policy as Jira.

Stretch goal; the system is fully functional with no integrations connected.

### 4.10 Frontend portal

A web dashboard served by the Main Process. Pages:

```
Login
  └─ Main Dash ─────────────── LOCAL AI panel (hardware, model, 0 cloud requests, 0 bytes uploaded)
       ├─ Current Tickets      live incident list, timeline per ticket, approve/deny for gated actions,
       │                       source badge + Jira key linking to the external issue
       ├─ Past Deployments     every executed action, with evidence → [Restore Backup]
       ├─ Logs Display         ticket events + service logs
       ├─ Overall Sys Health   per-service status from health checks
       │    └─ Health Check Scripts   registered checks, last result, enable/disable
       ├─ Integrations         Jira / Slack connection status, outbox queue, bytes sent, last sync
       └─ Settings             thresholds, baselines, allowlist, require_human_approval, Jira + Slack config
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

#### Hosting, data, and auth — decision: fully local

Supabase + Vercel + Auth0 were considered for the dashboard and rejected. All three are cloud services, and the dashboard would contradict the project's core claims:

- **Supabase** would hold tickets, diagnoses, and logs in a cloud Postgres — that *is* the infrastructure data the "0 bytes uploaded" panel says never leaves the box.
- **Vercel** would host the UI off-box, so the Internet-outage demo would take down OCE's own console.
- **Auth0** would make login depend on an external service during an outage.

What we use instead:

| Need            | Cloud option | Local choice                                                                                                                              |
| --------------- | ------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Dashboard host  | Vercel       | Server-rendered by the Main Process itself (Jinja2 templates). No separate frontend service.                                              |
| Data + realtime | Supabase     | OCE's own API (§7) + an SSE stream of `ticket_events` for live updates                                                                    |
| Login           | Auth0        | Local admin account(s), hashed password in `settings`, session cookie issued by the API. Stretch: self-hosted Authentik/Keycloak for SSO |

**Remote access (opt-in, off for the demo):** the API supports non-local clients, so a hosted UI could talk to the GX10 over a tunnel for remote operators. It is never the system of record — all state stays in the local DB.

#### Frontend stack: HTMX + Jinja2 + Alpine.js

No SPA framework and **no Node toolchain**. The dashboard is HTML rendered by FastAPI.

| Piece                                                     | Role                                                                                                                                                                      |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Jinja2**                                                | Page and fragment templates, rendered by FastAPI. Templates live next to the routes.                                                                                      |
| **HTMX**                                                  | Partial page updates (`hx-get`/`hx-post` on buttons, tables, forms). The SSE extension subscribes to `/tickets/{id}/stream` and swaps new timeline events in as agents work. |
| **Alpine.js**                                             | Client-side widgets: dropdowns, modals (confirm approve/deny/restore), tabs, toggles.                                                                                     |
| **Chart.js**                                              | Error rate and P95 charts, fed from `/api/tools/metrics`. Vendored locally.                                                                                               |
| **Pico CSS** or **Tailwind (CLI build, vendored output)** | Styling. Pico needs no build at all.                                                                                                                                      |

All JS/CSS is vendored into `oce/static/` so the dashboard works with the Internet disconnected.

Why this fits:

- The pages are mostly tables and detail views (tickets, deployments, logs, health checks) with a few actions. That is what HTMX is built for.
- Live agent output is the heart of the demo; HTMX SSE gives that in one attribute, no WebSocket client code.
- The same person owns the API and the UI, so there is no frontend/backend contract to negotiate.
- One fewer container on the GX10, nothing to build for ARM64.

HTML routes (browser only; JSON lives under `/api`, see §7):

```
GET  /                      → redirect to /dash (or /login)
GET  /login, POST /login    → session cookie
GET  /dash                  → full page
GET  /tickets               → full page; with HX-Request header returns only the table fragment
GET  /tickets/{id}          → full page; timeline fragment at /tickets/{id}/timeline
GET  /tickets/{id}/stream   → text/event-stream of rendered timeline rows
POST /tickets/{id}/approve  → returns updated gate-status fragment
POST /tickets/{id}/deny     → same
GET  /deployments, /logs, /health, /settings → same full-page / fragment pattern
```

---

## 5. Incident lifecycle

A ticket moves through these states. Each transition appends a `ticket_event`.

```
 detected ─▶ investigating ─▶ fix_proposed ─▶ sandbox_validated ─▶ reviewed ─▶ gate ─▶ awaiting_approval ─▶ approved ─▶ deploying ─▶ resolved
     │             │                │                 │                │          │            │                              │
     │             │                │                 │                │          │            └ human denies ─▶ denied       └ health worse ─▶ rolled_back
     │             │                │                 │                │          └ policy fails ─▶ denied (human may override)
     │             │                │                 │                └ Guardian NEEDS_TESTING ─▶ back to fix_proposed (with notes)
     │             │                │                 │                └ Guardian REJECT ─▶ back to investigating (max N attempts)
     │             │                │                 └ not reproduced / fix failed ─▶ back to investigating
     │             │                └ Engineer
     │             └ Investigator
     └ health script / Jira / Slack / manual
```

`awaiting_approval` is skipped when `settings.require_human_approval` is false (decision D3).

| # | State                 | Who                | What happens                                                                              |
| - | --------------------- | ------------------ | ----------------------------------------------------------------------------------------- |
| 1 | `detected`            | Health script / Jira / Slack / human | Check fails → ticket with `CheckResult` and metric deltas; or an external issue is imported with its description as the first event. |
| 2 | `investigating`       | Investigator       | Investigates with read-only tools; posts findings and a diagnosis to the timeline.        |
| 3 | `fix_proposed`        | Engineer           | Sandbox spun up; incident reproduced; patch generated and applied in sandbox.             |
| 4 | `sandbox_validated`   | Sandbox runner     | Load test + tests after the patch; compared against baseline.                             |
| 5 | `reviewed`            | Guardian           | Risk score + decision (`APPROVE` / `REJECT` / `NEEDS_TESTING`).                            |
| 6 | `gate`                | Orchestrator → gate | Orchestrator submits `ActionRequest`; gate runs policy checks.                           |
| 7 | `awaiting_approval`   | Human              | Dashboard shows the evidence; operator clicks Approve or Deny.                            |
| 8 | `approved`            | Gate               | Backup taken.                                                                             |
| 9 | `deploying`           | Run executor       | Staged rollout against production with monitoring.                                        |
| 10| `resolved`            | Health scripts     | Triggering check passes again; MTTR computed.                                             |

A human can intervene at any point from the dashboard (approve a denied action, deny an approved one, or restore a backup). Every transition also emits an outbound message to any linked external ticket (§4.9).

---

## 6. Data model

```sql
tickets
  id            TEXT PK        -- INC-0042
  source        TEXT           -- health_check | jira | slack | manual
  service       TEXT
  title         TEXT
  status        TEXT           -- see lifecycle
  severity      TEXT           -- low | medium | high | critical
  created_at    DATETIME
  resolved_at   DATETIME
  metrics_before JSON          -- baseline snapshot
  metrics_at_detect JSON       -- what tripped the check (NULL for externally reported tickets)
  external_ref  JSON           -- {"system": "jira", "key": "OPS-123", "url": "...", "last_synced_at": ...} or NULL

ticket_events                  -- the timeline; everything the dashboard shows comes from here
  id            INTEGER PK
  ticket_id     TEXT FK
  ts            DATETIME
  actor         TEXT           -- health_check | jira | slack | investigator | engineer | guardian | orchestrator | gate | executor | human
  type          TEXT           -- detected | reported | finding | diagnosis | patch | sandbox_result | review | action_request | gate_decision | human_decision | run | resolved | external_sync
  payload       JSON

integration_outbox             -- outbound messages to Jira/Slack; delivered by a background worker with retries
  id            INTEGER PK
  ticket_id     TEXT FK
  target        TEXT           -- jira | slack
  kind          TEXT           -- create_issue | comment | transition | message
  payload       JSON           -- already policy-filtered (D8); this is exactly what goes over the wire
  status        TEXT           -- pending | sent | failed
  attempts      INTEGER
  next_attempt  DATETIME
  bytes_sent    INTEGER        -- feeds the LOCAL AI panel counter

deployments
  id            TEXT PK        -- DEP-0081
  ticket_id     TEXT FK
  action        TEXT           -- allowlisted action name
  args          JSON
  backup_ref    TEXT           -- git tag / snapshot path
  status        TEXT           -- running | succeeded | failed | rolled_back
  started_at    DATETIME
  finished_at   DATETIME
  evidence      JSON           -- sandbox run id, risk score, guardian decision, test results

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
  value         JSON           -- thresholds, baselines, allowlist path, require_human_approval, users,
                               -- jira: {base_url, auth, project, jql, poll_interval_s, status_map, create_on_detect, outbound_detail}
                               -- slack: {token, channel, outbound_detail}
```

---

## 7. API reference

All JSON endpoints are under `/api`. The browser uses the HTML routes in §4.10 instead; everything else (Slack, health scripts, agents, external clients) uses these.

| Method | Path                                 | Used by             | Purpose                                                  |
| ------ | ------------------------------------ | ------------------- | -------------------------------------------------------- |
| GET    | `/api/health`                        | everyone            | OCE's own liveness                                       |
| GET    | `/api/tickets`                       | clients             | List tickets (filter by status)                          |
| POST   | `/api/tickets`                       | Slack, manual       | Create a ticket                                          |
| GET    | `/api/tickets/{id}`                  | clients, orchestrator | Ticket + full event timeline                           |
| POST   | `/api/tickets/{id}/events`           | agents, orchestrator | Append a finding / diagnosis / patch / review           |
| POST   | `/api/tickets/{id}/approve`          | human (via HTML route) | Approve an action awaiting approval                   |
| POST   | `/api/tickets/{id}/deny`             | human (via HTML route) | Deny an action                                        |
| POST   | `/api/actions`                       | orchestrator        | Submit an `ActionRequest` → command check gate           |
| GET    | `/api/deployments`                   | clients             | Past deployments with evidence                           |
| POST   | `/api/deployments/{id}/restore`      | human, executor     | Restore from that deployment's backup                    |
| GET    | `/api/checks`                        | clients             | Registered health checks and last results                |
| POST   | `/api/checks/{name}/run`             | human               | Run a check now                                          |
| PUT    | `/api/checks/{name}`                 | human               | Enable/disable, change interval                          |
| GET    | `/api/logs`                          | clients             | Ticket events + service logs                             |
| GET/PUT | `/api/settings`                     | human               | Thresholds, baselines, Slack, allowlist, approval mode   |
| GET    | `/api/stats/local-ai`                | dashboard           | Cloud requests (0), bytes uploaded (0), ticketing bytes sent, model, hardware |
| GET    | `/api/integrations`                  | dashboard           | Connection status, outbox depth, last sync per integration |
| POST   | `/api/integrations/jira/webhook`     | Jira (optional)     | Push alternative to polling; validated by shared secret   |
| POST   | `/api/integrations/jira/sync`        | human               | Force a poll + outbox flush now                           |
| POST   | `/api/tickets/{id}/link`             | human               | Link an existing local ticket to a Jira key              |

**Agent tool endpoints** — each `models/tools/*` function maps to one of these. Access is scoped per agent token.

| Method | Path                            | `models/tools` module | Investigator | Engineer | Guardian |
| ------ | ------------------------------- | --------------------- | :----------: | :------: | :------: |
| GET    | `/api/tools/logs`               | `docker_tools`        | ✓            |          |          |
| GET    | `/api/tools/containers`         | `docker_tools`        | ✓            |          |          |
| GET    | `/api/tools/metrics`            | `prometheus_tools`    | ✓            |          |          |
| GET    | `/api/tools/git`                | `git_tools`           | ✓            | ✓        |          |
| GET    | `/api/tools/code`               | `filesystem_tools`    | ✓            | ✓        |          |
| POST   | `/api/tools/sandbox/patch`      | `sandbox_tools`       |              | ✓        |          |
| POST   | `/api/tools/sandbox/test`       | `sandbox_tools`       |              | ✓        |          |
| POST   | `/api/tools/sandbox/loadtest`   | `sandbox_tools`       |              | ✓        |          |
| GET    | `/api/tools/diff`               | `review_tools`        |              |          | ✓        |
| GET    | `/api/tools/test-results`       | `review_tools`        |              |          | ✓        |
| GET    | `/api/tools/sandbox-results`    | `review_tools`        |              |          | ✓        |

**Auth:** a bearer token per client type for the hackathon — `agent-investigator`, `agent-engineer`, `agent-guardian`, `orchestrator`, `health`, `jira`, `slack`. Tokens are listed in `models/config.py` (agent side) and `settings` (server side). The dashboard uses a session cookie. Permissions are enforced by the server regardless of what the model asks for.

---

## 8. LLM serving and scheduling

**Serving:** Ollama at `http://localhost:11434`; `models/core/llm.py` calls `POST /api/chat` with `model: "nemotron3:33b"`. Verify before wiring agents:

```bash
ollama list
ollama ps
curl http://localhost:11434/api/tags
ollama run nemotron3:33b "Reply with READY."
```

**Scheduling:** all three agent roles share one loaded model; role-specific prompts, contexts, and tool sets differentiate them. The initial implementation processes agent requests **sequentially** (Investigator → Engineer → Guardian), which is also the natural order of the pipeline.

An earlier design proposed two concurrent request slots (Investigator + Guardian, then Engineer + Guardian). That remains an **optimization to benchmark**, not a known constraint: with one Ollama-served model the limit is concurrent requests and context size, not VRAM for multiple copies. A bounded semaphore and request queue can be added to the orchestrator once memory use, context sizes, and latency on the GX10 have been measured (roadmap A8).

---

## 9. Fake company (`fakeco/`)

The infrastructure OCA monitors. A miniature company in Docker Compose that behaves enough like real internal infrastructure to demonstrate the concept.

```
CUSTOMER ─▶ FRONTEND ─▶ ORDERS API ─▶ POSTGRES
                              └─────▶ REDIS
```

| Service      | Notes                                                                                                                                                                                                                                |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `orders-api` | FastAPI. Reads `DB_TIMEOUT` from config **committed in git** so the bad deploy is a real commit. Emits structured JSON logs, `/metrics` (prometheus_client), `/health`. Some queries `pg_sleep` ~150 ms so a 100 ms timeout actually fails. |
| `frontend`   | Optional thin web UI; can be skipped initially.                                                                                                                                                                                      |
| `postgres`   | Official image (multi-arch).                                                                                                                                                                                                         |
| `redis`      | Official image (multi-arch).                                                                                                                                                                                                         |
| `prometheus` | Scrapes `orders-api`.                                                                                                                                                                                                                |
| `loadgen`    | Steady synthetic traffic. **Required** — without traffic there is no error rate to watch.                                                                                                                                            |

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
6. Guardian review: risk 8/100, decision `APPROVE`.
7. Command gate checklist goes green → ticket shows **Awaiting approval** with the full evidence. Presenter clicks **Approve** (the one human action in the demo; can be switched off in Settings to show full autonomy).
8. Backup taken; run executor deploys in stages.
9. Health checks pass → **INCIDENT RESOLVED**, MTTR card. The linked Jira issue transitions to Done with the MTTR comment.
10. **Optional closer:** disconnect the GX10 from the Internet and re-run. Everything still works; Jira updates queue in the outbox and flush when connectivity returns.

**Jira variant** (shows the integration from the other direction): instead of step 2, a support engineer files `OPS-123: "Customers report checkout failing"` in Jira with the `oca` label. Within 30 s it appears in Current Tickets as `INC-0043` with the Jira key, Jira shows "OCA is investigating" + dashboard link, and the flow continues from step 4.

---

## 11. Repository layout

`models/` matches the current folder setup on the GX10. `oce/` and most of `fakeco/` are planned.

```text
oce/
├── README.md
├── requirements.txt               # single root requirements for models/ + oce/ (merge models/requirements.txt here)
├── fakeco/                        # demo company
│   ├── docker-compose.yml
│   ├── orders-api/
│   ├── frontend/
│   ├── loadgen/
│   ├── prometheus/
│   ├── inject_incident.sh
│   └── reset.sh
├── models/                        # AI subsystem (exists)
│   ├── __init__.py
│   ├── config.py                  # model, Ollama URL, Main Process URL, agent tokens, timeouts
│   ├── core/
│   │   ├── __init__.py
│   │   ├── llm.py                 # Ollama API client
│   │   ├── agent.py               # shared bounded agent loop
│   │   ├── tool_registry.py       # tool schemas, validation, per-agent allowlists, dispatch
│   │   └── state.py               # incident and agent context
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── investigator.py
│   │   ├── engineer.py
│   │   └── guardian.py
│   ├── prompts/
│   │   ├── investigator.txt
│   │   ├── engineer.txt
│   │   └── guardian.txt
│   ├── tools/                     # thin HTTP clients for oce /api/tools/*
│   │   ├── __init__.py
│   │   ├── docker_tools.py
│   │   ├── prometheus_tools.py
│   │   ├── filesystem_tools.py
│   │   ├── git_tools.py
│   │   ├── sandbox_tools.py
│   │   └── review_tools.py
│   ├── orchestrator.py            # handoffs, retries, ActionRequest submission
│   └── main.py                    # standalone dev entry point: python3 -m models.main --ticket INC-0042
├── oce/                           # platform (planned)
│   ├── main.py                    # FastAPI app, scheduler, launches orchestrator per ticket
│   ├── schemas.py                 # Pydantic contracts: Ticket, TicketEvent, ActionRequest, CheckResult …
│   ├── db.py                      # SQLite access layer
│   ├── api/                       # JSON routes under /api (tickets, deployments, checks, actions, tools, settings)
│   ├── web/                       # HTML routes for the dashboard (HTMX fragments + full pages)
│   ├── templates/                 # Jinja2: base.html, pages/, fragments/
│   ├── static/                    # vendored htmx.js, alpine.js, chart.js, css
│   ├── checks/                    # health check plug-ins
│   ├── sandbox.py                 # worktree + compose -p sandbox + load test
│   ├── gate.py                    # command check gate (policy engine)
│   ├── allowlist.yaml
│   ├── backup.py
│   ├── executor.py                # staged rollout + auto-restore
│   └── integrations/
│       ├── base.py                # TicketingIntegration interface, InboundTicket, OutboundMessage
│       ├── policy.py              # outbound content filter (D8): minimal / summary / off
│       ├── outbox.py              # background worker: retry delivery, count bytes
│       ├── jira.py                # JQL polling, webhook parsing, issue create/comment/transition (httpx)
│       └── slack.py
└── docs/
```

Run the agents standalone from the repo root with `python3 -m models.main` so `models.core` and `models.agents` imports resolve.

---

## 12. Tech stack

| Layer          | Choice                                   | Why                                                                          |
| -------------- | ---------------------------------------- | ---------------------------------------------------------------------------- |
| Hardware       | ASUS Ascent GX10 (GB10, ARM64, 128 GB)   | Competition platform; everything must be ARM-compatible                      |
| Model          | `nemotron3:33b` (initial, local)         | One shared model across three agent roles                                    |
| Model serving  | Ollama, local `/api/chat`                | Already installed on the box                                                 |
| Backend        | Python 3.12, FastAPI, SQLite             | Fast to build, async, ARM-friendly                                           |
| Infra target   | Docker Compose (multi-arch images)       | fakeco + sandbox                                                             |
| Metrics        | Prometheus + prometheus_client           | Standard, lightweight                                                        |
| Logs           | Structured JSON to stdout / `docker logs`| Skip Loki unless time allows                                                 |
| Dashboard      | HTMX + Jinja2 + Alpine.js, Chart.js      | Server-rendered by FastAPI; no Node, no build step, works offline (see §4.10) |
| Auth           | Local accounts, session cookie           | Must work offline; no external IdP                                           |
| Ticketing      | Jira REST API v2 via `httpx`; Slack Web API | Mirror only; policy-filtered outbound; outbox so it never blocks (§4.9)   |
| Load gen       | Small Python script or Locust            |                                                                              |

---

## 13. Work split and roadmap

**AI subsystem (`models/`):** Ollama client, bounded agent loop, role prompts, tool clients, agent state, the three agents, orchestration.

**Platform (`oce/`, `fakeco/`):** API, database, dashboard, health checks, gate, backup, executor, sandbox, fakeco, Jira integration, optional Slack.

Both tracks depend on the shared contracts in `oce/schemas.py` (`Ticket`, `TicketEvent`, `ActionRequest`, `CheckResult`, agent output shapes) — agree on those first.

### Platform milestones

| Stage  | Deliverable                                                                                          | Unblocks           | Status  |
| ------ | ---------------------------------------------------------------------------------------------------- | ------------------ | ------- |
| **M0** | `oce/schemas.py` contracts + stub `/api` returning canned data + `/api/tools/*` stubs                 | AI team, dashboard | Next    |
| **M1** | `fakeco/` up with loadgen; `inject_incident.sh` visibly raises error rate in Prometheus              | everything         | Planned |
| **M2** | Health checks + ticket creation + SSE timeline. Inject → ticket appears live on the dashboard.        | A3                 | Planned |
| **M3** | Gate + allowlist + backup + executor + approve/deny. Manual `POST /api/actions` restores the timeout. | A5, A6             | Planned |
| **M4** | Sandbox runner with before/after metrics behind `/api/tools/sandbox/*`.                              | A4                 | Planned |
| **M5** | Jira integration: inbound polling → ticket, outbound comments/transitions via outbox, policy filter, Integrations page. | demo (Jira variant) | Planned |
| **M6** | Remaining dashboard pages; Slack (stretch).                                                          | demo               | Planned |
| **M7** | End-to-end with all three agents; MTTR card; offline demo (outbox flush on reconnect).               | judging            | Planned |

### AI milestones

| Stage  | Deliverable                                                                       | Status                                      |
| ------ | --------------------------------------------------------------------------------- | ------------------------------------------- |
| **A0** | Create `models/` structure and discover installed Ollama models                    | Done — folder created, model tags confirmed |
| **A1** | Test `nemotron3:33b` inference; implement `config.py` and `core/llm.py`           | Next                                        |
| **A2** | Tool registry and bounded tool-calling agent loop                                  | Planned                                     |
| **A3** | Investigator: logs, container state, metrics, git, read-only diagnosis            | Planned (needs M0 stubs)                    |
| **A4** | Engineer: sandbox-scoped patching, reproduction, test iteration                    | Planned (needs M4)                          |
| **A5** | Guardian: evidence review, risk evaluation, advisory decision                      | Planned                                     |
| **A6** | Orchestrator: handoffs, retries, `ActionRequest` submission, audit trail           | Planned (needs M3)                          |
| **A7** | Integrate with fakeco, API, gate, backup, executor, dashboard                      | Planned                                     |
| **A8** | End-to-end demo, latency profiling, concurrency benchmark, offline and safety tests | Planned                                     |

Early checks:

- Test on the GX10 early — all images must be ARM64.
- Never allow arbitrary shell commands or production filesystem writes through agent tools.

---

## 14. Decisions

Recorded so nobody re-litigates them by accident. Any can be overturned by the team — update this section if so.

| ID     | Decision                                                                                                                                                                                                           |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **D1** | **Agent tools are HTTP clients.** `models/tools/*` call `oce` `/api/tools/*`. The registry controls which tools an agent is offered; per-agent API tokens control what the server will actually serve.          |
| **D2** | **Guardian is advisory.** Its `APPROVE` never reaches the gate directly. `models/orchestrator.py` builds the `ActionRequest` and posts it to `/api/actions`.                                                       |
| **D3** | **Human approval on by default.** `settings.require_human_approval = true`. After the gate's policy checks pass, the ticket waits for an Approve click. Can be switched off in Settings for a fully autonomous run. |
| **D4** | **Single process.** `oce/main.py` runs the orchestrator as an in-process background task. `models/main.py` is a standalone dev entry point only.                                                                  |
| **D5** | **Fully local frontend.** HTMX + Jinja2 + Alpine served by FastAPI; local auth. No Vercel / Supabase / Auth0 (§4.10).                                                                                             |
| **D6** | **Sequential agent execution first.** Concurrency is an A8 benchmark, not a day-one feature (§8).                                                                                                                 |
| **D7** | **Naming.** Top-level `models/` is the agent package; platform data contracts live in `oce/schemas.py` to avoid the clash. One root `requirements.txt`.                                                            |
| **D8** | **External ticketing is mirror-only and policy-filtered.** Jira/Slack never hold evidence and never receive logs, code, diffs, or metrics. Outbound text is templated from whitelisted fields; `outbound_detail` defaults to `minimal` for Jira Cloud, `summary` for self-hosted. Delivery goes through a retrying outbox so an unreachable Jira never blocks the pipeline. Approve/deny is dashboard-only; Jira comments are never instructions. |

---

## 15. Open questions

- Does the competition specifically require Nemotron 3.5 Lightning, and does any installed Ollama tag satisfy it? Don't relabel `nemotron3:33b` until confirmed with organizers.
- How many concurrent model requests can Ollama serve reliably on this GX10 at the needed context length? (A8)
- Sandbox isolation details: network, mounts, credentials — what exactly stops a sandbox container reaching production?
- Real traffic-split canary (nginx weights) or demo-only visual stages?
- Log source: `docker logs`, shared log file, or a dedicated log system?
- Jira for the demo: a free Jira Cloud site (easy, but external → `minimal` policy and the panel shows bytes sent) or a self-hosted Jira Data Center trial in Docker on the GX10 (fully local, heavier to set up, ARM64 support to verify)?
- Which Jira field carries `service`? Component vs. a custom field vs. parsed from the summary.
- Slack: in the demo or stretch only?

## Implementation notes — agent checkpoint

- Implemented environment-backed model configuration, the async Ollama client,
  and inference/synthetic tool-call smoke checks; real GX10 validation is pending.
- Implemented the three role classes in `models/agents/` with separate prompts,
  role-scoped tool catalogs, and fresh per-run histories.
- Added the shared bounded tool-using loop, JSON argument/output validation, and
  structured observations/results in `models/core/`.
- Added local HTTP clients for the documented `/api/tools/*` endpoints. Investigator
  is read-only, Engineer has sandbox tools, and Guardian has read-only review tools.
  No agent submits production actions; Guardian approval remains advisory.
- Added tests for role boundaries, invalid arguments, incident scoping, tool-result
  feedback, output correction, run limits, and isolated histories.
- Main Process endpoints, server-side role permissions/sandbox isolation, shared
  platform schemas, orchestration, event persistence, and live GX10 validation
  remain pending. See `models/README.md` for setup and provisional tool contracts.
