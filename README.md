# OCA — On-Call Agent

**A fully local autonomous SRE team.** OCA monitors a company's private infrastructure, detects incidents, diagnoses the root cause, develops a fix, proves it in an isolated sandbox, and deploys it under deterministic policy control — without source code, logs, credentials, or infrastructure data ever leaving the machine.

Everything runs on a single ASUS Ascent GX10 (NVIDIA GB10, 128 GB unified memory) using Ollama to serve one shared local Nemotron model to three specialized agents. The platform (`oca/`) and the agents (`models/`) are implemented; the whole pipeline runs end to end against a deterministic fake company on a laptop (`OCA_RUNNER=dry`) and against the real Docker Compose company on the GX10 (`OCA_RUNNER=real`).

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
   - [Frontend portal](#411-frontend-portal)
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

The agent code lives in the top-level `models/` Python package (stdlib only); the platform lives in `oca/` (FastAPI); the company under test, Driftwood, lives in **its own git repository** that OCA reaches through `OCA_FAKECO_ROOT` (its ops scripts need their own repo with a `baseline` tag and the incident branches; it was moved out of this tree).

```text
                    ASUS ASCENT GX10 (GB10 · 128 GB unified memory)
┌────────────────────────────────────────────────────────────────────────┐
│ Browser dashboard / health checks / Jira · Slack / external clients    │
│                              │                                         │
│                     oca/  Main Process / API  (FastAPI, :8001)         │
│      Incident state (SQLite) · scheduler · gate · sandbox · executor   │
│                              │  HTTP (orchestrator token)              │
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
│                  models/core/agent.py (bounded agent loop)              │
│                  models/core/tool_registry.py                           │
│                  models/tools/catalog.py → HTTP → oca /api/tools/*     │
│                              │                                         │
│               models/core/llm.py → Ollama :11434                       │
│                     nemotron3:33b (native tool calls)                  │
│                                                                        │
│  oca/fakeco/  FakecoRunner ──► SubprocessRunner (real: ops/*.sh, git, │
│                             │                   docker compose)        │
│                             └► FakeRunner (dry: deterministic model   │
│                                            of both driftwood incidents)│
│                                                                        │
│  Guardian review → orchestrator → command gate → (human) → backup      │
│                                   → executor → production → verify     │
└────────────────────────────────────────────────────────────────────────┘
```

**Control boundaries:** An LLM chooses among registered tools; Python validates arguments, enforces per-agent tool allowlists, and records the results. Investigator cannot write. Engineer can write only to a sandbox worktree, through a server that validates every diff. Guardian can review but cannot deploy. The deterministic command gate and executor — not the LLM — control production changes, and by default a human approves each deployment from the dashboard (see [§14](#14-decisions)). Everything that touches the company under test goes through one `FakecoRunner` seam, so the same pipeline runs with Docker (`real`) or without it (`dry`).

**Local model inventory observed on the GX10** (`ollama list`):

| Ollama model              | Approx. disk size | Role                                        |
| ------------------------- | ----------------: | ------------------------------------------- |
| `nemotron3:33b`           |             27 GB | Shared model for all three agents (native tool calling verified via `/api/chat`) |
| `nemotron-3-super:latest` |             86 GB | Larger Nemotron candidate to benchmark later |
| `qwen3.6:35b`             |             23 GB | Alternative model for evaluation            |
| `gemma4:26b`              |             17 GB | Alternative model for evaluation            |

On a development Mac, `OCA_MODEL=gpt-oss:latest` (20B, tool-capable) drives the same pipeline against the dry runner; a full detect → diagnose → patch → prove → review → gate run takes about a minute.

---

## 4. Components

### 4.1 Main Process / API

The central service. Owns the database, exposes the REST API, serves the dashboard, runs the health-check scheduler, orchestrates the incident pipeline, and hosts the command gate.

Responsibilities:

- Serve the JSON API under `/api` on port **8001** (driftwood owns 8000/8080; see [§7](#7-api-reference)) to Slack, health scripts, agents, and external clients, and the HTML dashboard at `/` (see [§4.11](#411-frontend-portal)).
- Run registered health checks on a timer and open tickets when they fail.
- Drive each ticket through the [incident lifecycle](#5-incident-lifecycle) by launching `models.orchestrator` as a background task per ticket.
- Route every proposed action to the command check gate; never execute anything directly.
- Append every state change to the ticket's event timeline so the dashboard and logs are always derivable from the DB.
- Mirror ticket state to external ticketing systems (Jira, Slack) through a retrying outbox, so a ticketing outage never blocks the pipeline.

Implementation: Python, FastAPI, async (`oca/main.py`, `python3 -m oca.main`). **Single process.** `oca/pipeline.py` runs `models.orchestrator` in-process as a background task per ticket; the orchestrator and the agents reach the platform over HTTP to `localhost:8001` with their own tokens, so the same code also runs standalone (`python3 -m models.main --ticket INC-0042`). Configuration is `OCA_*` environment variables (see `.env.example`); runtime policy (approval mode, risk threshold, baseline) lives in the `settings` table and is editable from the dashboard.

### 4.2 Database

SQLite (`data/oca.db`, WAL mode, one aiosqlite connection with a single writer lock). Schema is `oca/schema.sql`, applied idempotently on startup; all access goes through `oca/db.py`. Every event append is also published on an in-process bus that feeds the SSE streams.

### 4.3 Health status scripts

Deterministic, pluggable checks in `oca/checks/`. **No LLM involved.** Each check implements the same interface:

```python
class HealthCheck(ABC):
    name: str
    service: str
    interval_s: int
    opens_ticket: bool = True

    async def run(self) -> CheckResult: ...

class CheckResult(BaseModel):
    ok: bool
    check: str
    service: str
    detail: str               # human-readable summary
    metrics: dict[str, float] # e.g. {"error_rate_pct": 25.3, "p95_latency_ms": 10234}
    breaches: list[str]       # which SLOs tripped
    raw: dict | None          # full status snapshot, attached to the ticket
```

| Check                   | What it does                                                                                  | Opens a ticket when                     |
| ----------------------- | --------------------------------------------------------------------------------------------- | --------------------------------------- |
| `status_check`          | Runs driftwood's own detector, `ops/status.py --json` (SLO: 5 % 5xx, 1 s p95, 90 % pool)       | `healthy == false` — **primary detector** |
| `endpoint_check:*`      | HTTP GET on `orders-api /health` and `frontend /api/health`                                   | non-2xx or timeout on **two consecutive runs** (orders-api only; one refused connection during a redeploy is ignored) |
| `metrics_check`         | Recorded Prometheus series vs the learned baseline × factor and the SLO                      | evidence only                           |
| `log_error_parse`       | Tails `logs/orders-api.log`, share of 5xx in a 60 s window                                    | evidence only                           |
| `jira_ticket_reader`    | Polls Jira with a JQL filter (see §4.9)                                                       | enabled while Jira is connected in Settings |
| `slack_ticket_reader`   | Polls a Slack channel for incident reports                                                    | registered, disabled (stretch)          |

The scheduler (`oca/checks/scheduler.py`) runs due checks every tick, stores every result in `health_checks`, and opens **one ticket per failing service**: further failures append a rate-limited `check_failed` event instead of a new ticket, and a service that fails again within `reopen_cooldown_s` of being resolved does not reopen. While healthy, the baseline (`settings.baseline`) is learned with an exponential moving average. Thresholds live in `settings.thresholds`.

### 4.4 LLM agents

The AI implementation is contained in `models/` and comprises **three separate tool-using agents**, not three prompt templates. Each loads a role-specific prompt from `models/prompts/`, is granted a subset of the tools in `models/tools/`, and runs the shared bounded loop in `models/core/agent.py`: decide → call a tool → observe → continue or return.

| Agent            | Source + prompt                                  | Responsibilities                                                                                          | Tool access                                                         | Forbidden                                            |
| ---------------- | ------------------------------------------------ | --------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------- | ---------------------------------------------------- |
| **Investigator** | `agents/investigator.py` + `prompts/investigator.txt` | Examine logs, Prometheus metrics, container state, git history, and relevant code; report suspected root cause with evidence | Read-only: `docker_tools`, `prometheus_tools`, `filesystem_tools`, `git_tools` | Editing files, commands with side effects, deployment |
| **Engineer**     | `agents/engineer.py` + `prompts/engineer.txt`    | Fix the incident in the sandbox copy, run load test and tests, iterate                                   | `get_code`, `list_files`, `get_git`; **sandbox-scoped** write tools `set_config`, `create_migration`, `apply_patch`; `run_tests`, `run_loadtest` | Editing production, deployments, unrestricted shell  |
| **Guardian**     | `agents/guardian.py` + `prompts/guardian.txt`    | Inspect proposed diff, test evidence, operational/security risk, and policy constraints; recommend a decision | Read-only: `review_tools` (diff, test results, risk evaluation)      | Modifying patches, calling the gate, deploying       |

**Tool architecture (decision D1):** `models/tools/catalog.py` declares every tool once — name, description, JSON schema, allowed roles, and the `/api/tools/...` endpoint it maps to — and `models/tools/client.py` is the stdlib HTTP transport. Permissions are enforced twice:

1. `tool_registry.py` decides which tools an agent is *offered* (the model cannot name a tool it was not given).
2. Each agent runs with its own API token; the Main Process only serves the tool endpoints that token is scoped to ([§7](#7-api-reference)). Even a confused or manipulated agent cannot reach a tool outside its role.

**Shared implementation:**

- `models/config.py` — `ModelConfig` from `OCA_MODEL`, `OCA_OLLAMA_URL`, `OCA_LLM_TIMEOUT_S`, `OCA_MODEL_KEEP_ALIVE` (loopback URLs only).
- `models/core/llm.py` — non-streaming client for Ollama's local `/api/chat` with native tool calls; no proxies, no redirects, no retries.
- `models/core/agent.py` — bounded decide → tool → observe loop with step/time/observation budgets, cross-ticket guard, JSON final answer validated against the role's schema (a repair turn uses Ollama structured output), and an `on_observation` hook that streams tool use onto the ticket timeline. Loop guards for small local models: an identical tool call is refused until a write succeeds, every tool result carries `steps_remaining`, a budget nudge fires near the end, and the Engineer cannot finish without a successful write (unless it reports `reproduced: false`). A failed attempt hands the next one a briefing of what was tried and why it failed.
- `models/core/tool_registry.py` — JSON-schema-subset validation, per-role allowlists, dispatch.
- `models/core/state.py` — the three output contracts (`DIAGNOSIS_SCHEMA`, `PATCH_SCHEMA`, `REVIEW_SCHEMA`) mirrored 1:1 by `oca/schemas.py` (`tests/test_contracts.py` enforces it).
- `models/orchestrator.py` — Investigator → Engineer → Guardian handoffs with feedback, retries (`OCA_MAX_ATTEMPTS`), `NEEDS_TESTING` loops, and submission of the final `ActionRequest` to `POST /api/actions` (decision D2). Talks to the platform only over HTTP with the orchestrator token.
- `models/main.py` — `--check [--tools]` smoke-tests the model; `--ticket INC-0042` runs the pipeline standalone against a running Main Process.

`models/` is **stdlib only** and never imports `oca/`; the platform conforms to the agent contracts, not the other way round. Agent-side tests: `python3 -m unittest discover -s models/tests` (scripted models, fake APIs).

A **shared model does not mean shared permissions**: each agent has its own prompt, execution history, and allowed tools. Tool outputs such as logs and repository contents are untrusted data and must not be treated as instructions.

**Agent output contracts:**

- **Investigator:** structured diagnosis, evidence references, confidence, unresolved questions.
- **Engineer:** proposed patch/diff, sandbox reproduction evidence, test commands, before/after results.
- **Guardian:** risk score (0–100), policy concerns, decision (`APPROVE`, `REJECT`, `NEEDS_TESTING`), justification. `APPROVE` is **advisory**; the orchestrator turns it into an `ActionRequest`, and only the gate (and, when `require_human_approval` is on, a human) can approve execution.

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

An isolated copy of the relevant application services where the Engineer works and fixes are proven. **OCA owns the full sandbox lifecycle** through `oca/sandbox.py` and `oca/environments/compose.py`; it no longer calls Driftwood's `ops/sandbox.sh` or `ops/reset_db.sh`. The Engineer reaches it only through `sandbox_tools` → `/api/tools/sandbox/*`. **All measurements are taken by the server**; the gate and Guardian trust `sandbox_runs`, never the Engineer's own claims.

Mechanism:

1. `prepare` (orchestrator): OCA tears down any previous sandbox (including its volumes), then creates a detached git worktree at the running production SHA under `OCA_DATA_DIR/sandboxes/<identity>/checkout-<cycle>` (a new name per sandbox: Docker Desktop on macOS refuses a bind-mount path that was deleted and recreated). It resolves the company's Compose file with an explicit OCA environment, validates it, and writes a standalone sandbox configuration. OCA starts a project named `oca-sandbox-driftwood-<identity>` with fresh synthetic seed data and runs the company's load generator to record **BEFORE**. The incident must reproduce (SLO breach), or the ticket goes back to the Investigator.
2. Engineer write tools. `set_config(ticket_id, key, value)` and `create_migration(ticket_id, name, sql)` take structured arguments and the **server writes the diff** (one `KEY=value` line in `app.env`; the next numbered `migrations/NNN_<name>.sql`), which removes the hunk-arithmetic failures small models make. `apply_patch(ticket_id, diff)` remains for code changes: the server validates the unified diff (`services/orders-api/**` or `services/frontend/**`, never `Dockerfile`/`requirements.txt`, ≤ 20 files, ≤ 2000 lines). All three create or reuse branch `fix/<ticket>` in the worktree, check, apply and commit the patch, and rebuild the sandbox. Patches are cumulative. The production checkout is not edited by sandbox operations. Every write tool records the diff, the commit and the shell commands it ran on its `finding` event; `run_tests` and `run_loadtest` record their commands and output too, so the ticket page shows exactly what the Engineer did.
3. Engineer `run_loadtest` / `run_tests` iterate; `validate` (orchestrator) is authoritative: rebuild on `fix/<ticket>`, record **AFTER**, and run the **patched checkout's** tests in an OCA test container against the sandbox API. Compare with the baseline and SLO. Measurements and test output are attached as `sandbox_result` events and a `sandbox_runs` row.

**The sandbox judges by the production detector's SLO set**, not only by what the load generator sees: after each load test OCA reads peak DB pool utilisation and DB error rate from the sandbox's own Prometheus and applies the same thresholds (5 % errors, 1 s p95, 90 % pool). A saturated pool with 0 % errors reproduces, and a fix that leaves the pool saturated is not proven. **Load matches production**: the generator runs at 1.5× the request rate production showed at detection (30–200 rps) for at least 40 s, so load-driven incidents reproduce and Prometheus has samples from inside the test window.

If the incident still does not reproduce, the run stops after that attempt instead of re-investigating: `prepare` attaches the live production SLO state, and the ticket is failed with a plain-language hand-off ("production still breaches db_pool_utilization_pct; the cause is probably outside the application code" or "production has recovered; likely transient"). The Investigator's diagnosis stays on the ticket and in the report for the human who takes over.

```
                BASELINE      BEFORE FIX     AFTER FIX
Error rate       0.0%          25.0%          0.0%   ✓
P95              12 ms         10012 ms       16 ms  ✓
Tests            PASS          FAIL (1)       PASS   ✓
```

The sandbox is torn down after the ticket resolves (or when the pipeline gives up). Failed startup or initial measurement triggers cleanup. Cleanup failures retain ownership records for a retry and are reported instead of returning a false success. One sandbox runs at a time. OCA uses separate named volumes, an internal bridge network for all services plus a second non-internal bridge that only the API, frontend and Prometheus join (Docker does not bind host ports on internal networks), loopback-only published API/frontend/Prometheus ports, and a generated database password; it never copies the company's `.env`. Non-log bind mounts must resolve inside the sandbox checkout and are made read-only; log mounts are redirected to OCA's runtime directory. External resources, host networking, privileged services, external logging drivers, secrets/configs, and builds outside the checkout are rejected. Containers still share the host kernel: this is not a VM boundary. The operator must review the company's Dockerfiles, seed scripts, and profile. In dry mode `FakeRunner` models both incidents without Docker.

#### Company profile and local setup

The initial supported environment is **Driftwood on Docker Compose**, not arbitrary company infrastructure. `oca/environments/profiles/driftwood.json` supplies the Compose path, test path, services, load-test URL, sandbox ports, environment allowlists/overrides, and log targets. A custom operator-owned JSON profile can be selected with `OCA_SANDBOX_PROFILE`; agents cannot edit it. The platform's production checks, patch policy and deploy adapter are still Driftwood-specific, so a profile alone does not make the entire platform company-independent.

```bash
# Run from the OCA repository. The company clone keeps its ordinary deployment files.
export OCA_RUNNER=real
export OCA_FAKECO_ROOT=/path/to/FakeCo_Driftwood
export OCA_DATA_DIR=/path/to/oca/data    # must be outside the company checkout
# Optional: export OCA_SANDBOX_PROFILE=/path/to/operator-profile.json
python3 -m oca.main
```

Requirements: Git, Docker Engine and Docker Compose v2 supporting `config --format json` and `up --wait`, plus a committed company checkout with build files, tests and synthetic seed data. OCA retains its existing `ops/status.py` and `ops/deploy.sh` production integrations; no company sandbox scripts or `sandbox.env` are needed. Database reset recreates **all sandbox volumes** from synthetic seeds, so it is slower than the former index-only reset. Production volumes are never reset.

The test container uses OCA's `oca/environments/test_runner/Dockerfile` (Python 3.12, pytest and httpx). Build it while online, along with the company's app/loadgen images and pull the base images, before an offline demo:

```bash
docker build -t oca-sandbox-tests:1 oca/environments/test_runner
```

Then rehearse a complete OCA sandbox prepare/patch/validate/teardown while online to warm application build caches, and repeat offline. The OCA runtime stores generated credentials locally under the host account's filesystem permissions. Teardown removes runtime files and containers/volumes; patch branches and cached images remain available for deployment and subsequent runs. Existing company-created `driftwood-sandbox` resources are not adopted or removed by OCA; tear down that old sandbox with the old script before switching if its ports are occupied.

Local verification covers real git worktrees, mocked Docker command execution, isolation rejection, failure cleanup and the dry incident pipeline. **Real Docker execution and both incident scenarios on the GX10 remain to be verified.**

### 4.6 Command check gate

The single choke point between AI and the real system. **Deterministic, no LLM.** Lives in `oca/gate.py`; `evaluate()` is a pure function, unit-tested check by check.

Every action request has the shape:

```json
{
  "action": "set_config",
  "args": { "service": "orders-api", "key": "DB_TIMEOUT_MS", "value": 3000 },
  "ticket_id": "INC-0042",
  "requested_by": "orchestrator",
  "evidence": { "sandbox_run_id": "SBX-0001", "risk_score": 8, "guardian_decision": "APPROVE", "tests_passed": true }
}
```

The `evidence` block only *selects* rows; the gate reloads the sandbox run and the Guardian's `review` event **from the database** and never trusts the request body. The checks, in order:

```
┌─ POLICY ENGINE ──────────────────────────┐
│ 1. Action is on the allowlist?        ✓  │
│ 2. Args match the action's schema?    ✓  │
│ 3. Args within allowed values?        ✓  │  (enum / int range / regex)
│ 4. Sandbox reproduced + fixed?        ✓  │
│ 5. Tests passed?                      ✓  │
│ 6. Guardian decision == APPROVE?      ✓  │
│ 7. Risk score below threshold?        ✓  │
│ 8. Not on the blocklist / cooldown?   ✓  │
└──────────────────────────────────────────┘
   → POLICY PASS → if require_human_approval: awaiting_approval (dashboard button)
                   else: approved
   → APPROVED    → backup → run executor
   → DENIED      → ticket `denied`; the dashboard offers "Override & Approve" (recorded as a human override)
```

The allowlist is `oca/allowlist.yaml`, **named, parameterised commands** — the LLM never writes raw shell:

```yaml
actions:
  deploy_patch:        # git merge --no-ff fix/<ticket> && ops/deploy.sh
    args: { ticket_id: {type: str, pattern: "^INC-\\d{4,}$"}, branch: {type: str, pattern: "^fix/INC-\\d{4,}$"} }
    requires: [backup, sandbox_reproduced, sandbox_fixed, tests_pass, guardian_approve, risk_below]
  set_config:          # edit config/app.env, commit, ops/deploy.sh
    args:
      service: { enum: [orders-api] }
      key:     { enum: [DB_TIMEOUT_MS, DB_POOL_MAX, DB_POOL_ACQUIRE_TIMEOUT_MS] }
      value:   { type: int, min: 100, max: 30000 }
    requires: [backup, sandbox_reproduced, sandbox_fixed, guardian_approve, risk_below]
  restart_service:
    args: { service: { enum: [orders-api, frontend] } }
    requires: [backup]
  rollback:
    args: { deployment_id: { type: str, pattern: "^DEP-\\d{4,}$" } }
    requires: [backup]
```

The orchestrator picks `set_config` when the proven diff changes exactly one allowlisted key in `app.env`, and `deploy_patch` otherwise. Everything the gate does — passed, denied, awaiting, approved — is written to `ticket_events` and the `actions` ledger; approvals create `deployments`.

### 4.7 Backup system

`oca/backup.py`, called by the executor after approval and before anything changes:

- Tag the current production commit in the driftwood clone: `backup/<deployment_id>`.
- Snapshot the deploy record and `config/app.env` to `data/backups/<deployment_id>/`.
- Record `backup_ref` on the deployment and a `run{stage: snapshot}` event.

**Restore** = `git reset --hard backup/<id>` on `main` plus `ops/deploy.sh` (the same approach as driftwood's `ops/reset.sh`). Exposed as `POST /api/deployments/{id}/restore` and the **Restore Backup** button on the Past Deployments page, and triggered automatically when post-deploy health is worse. A restore is itself recorded as a `rollback` deployment. Note: restoring code does not undo a DB migration already applied (the loyalty index stays); `ops/reset_db.sh` is the manual remedy.

### 4.8 Run executor

**Verification is three-way.** Before the change the executor snapshots production's SLO metrics. After the staged rollout it waits `OCA_VERIFY_SETTLE_S` (60 s: the detector's series are 1-minute rates and still hold pre-deploy samples) and then runs the primary check until it has passed `OCA_VERIFY_RUNS` times in a row (at most twice that many reads): healthy → `resolved`; a breaching metric more than 10 % above its pre-deploy value, or a new breach → automatic rollback; improved (at least one SLO metric 20 % better, none worse) but still breaching → the deployment is **held** and the ticket moves to `awaiting_verification`. A correct fix under external load is no longer rolled back for failing to reach the absolute SLO, and OCA never declares an incident resolved while production is still breaching: the operator clicks **Confirm resolved** or **Roll back** on the ticket page (`POST /api/tickets/{id}/confirm` / `/rollback`).

Executes an approved action against the production stack. It knows how to perform each allowlisted action and nothing else. Lives in `oca/executor.py`; runs as a background task so the approve click returns immediately.

```
snapshot → canary 10% → monitor → widen 50% → monitor → full 100% → verify ×N
```

Driftwood has no traffic splitting, so the real change happens at `canary` (`ops/deploy.sh`) and the following stages are health polls; the stage events are what the timeline shows. `verify` re-runs the primary health check `OCA_VERIFY_RUNS` times. All green → deployment `succeeded`, ticket `resolved` with the MTTR card, sandbox torn down. Still failing, or an error rate above the one at detection → **auto-restore** from the backup, deployment `rolled_back`, ticket `rolled_back`. Any exception → `failed` with an `error` event.

### 4.9 Ticketing integrations (Jira, Slack)

OCA connects to the ticketing systems a company already uses, so an incident can enter from a support ticket and the people watching that ticket see it progress and close. All integrations share one shape, implemented in `oca/integrations/`:

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

**Configure it** on the dashboard: Settings → Integrations (base URL, account email, API token, project key, issue type, outbound detail, status map, inbound JQL), then *Test connection*. `OCA_JIRA_*` environment variables seed the same fields on first boot. `python3 -m oca.integrations.jira --doctor` checks credentials from a shell. Every delivery is recorded on the ticket timeline as an `external_sync` event with the byte count; `GET /api/integrations` and `GET /api/integrations/outbox` expose the same ledger.

#### Slack

Same interface, secondary priority:

- **Inbound:** `slack_ticket_reader` polls a channel; a matching message creates a ticket (`source: slack`).
- **Outbound:** state-change messages to a channel, same `outbound_detail` policy as Jira.

Stretch goal; the system is fully functional with no integrations connected.

### 4.10 Incident reports

Every ticket that ends (`resolved`, `failed`, `rolled_back`) gets a post-incident report, written by `oca/reports.py` from the ticket's own events. **No LLM composes it**: the Investigator's diagnosis and the Guardian's justification are quoted as written, and every number is a server measurement. Sections: summary, impact (baseline vs detection), timeline, root cause with evidence refs, fix (diff and production deployment), validation table, review and approval (Guardian, gate checklist, operator decision), follow-ups (open questions, Guardian concerns, errors).

The Markdown file is written to `OCA_DATA_DIR/reports/<ticket>.md` and a `report` event is appended to the ticket. The dashboard renders it at `/tickets/<id>/report` (download at `/tickets/<id>/report.md`; the API serves the Markdown at `/api/tickets/<id>/report`). Open tickets show a live preview. Connected Jira issues get a comment and Slack a message carrying the report **link**; at the `summary` detail level the one-line outcome is included. The report body itself never leaves the machine (D8).

### 4.11 Frontend portal

A web dashboard served by the Main Process (`oca/web/`, `oca/templates/`). Pages:

```
Login
  └─ Main Dash ─────────────── LOCAL AI panel (hardware, model, 0 cloud requests, 0 bytes uploaded)
       ├─ Current Tickets      live incident list, timeline per ticket, approve/deny for gated actions,
       │                       source badge + Jira key linking to the external issue, incident report
       │                       (Markdown, written when the ticket closes)
       ├─ Past Deployments     every executed action, with evidence → [Restore Backup]
       ├─ Logs Display         ticket event log (paged: rows per page + pages; the ticket timeline pages the same way)
       ├─ Overall Sys Health   per-service status from health checks
       │    └─ Health Check Scripts   registered checks, last result, enable/disable, Run now / Run all
       ├─ Integrations         Jira / Slack connection status, outbox queue, bytes sent, last sync
       └─ Settings             policy (require_human_approval, risk threshold, cooldown, blocklist), dashboard
                               time zone, model dropdown (any model installed in Ollama; applies to the next run),
                               Integrations (Jira / Slack forms with Test connection), Shut down server
```

The ticket page shows the live timeline plus an **Engineer's changes** panel: every applied patch with its coloured diff, and the complete log of shell commands the sandbox tools ran. Timeline rows for `set_config`, `create_migration` and `apply_patch` expand to the diff; `run_tests` rows show the pytest command and output tail. The **Shut down** button (sidebar and Settings) stops the OCA process gracefully after a confirm; production and the sandbox containers are left untouched.

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
- **Vercel** would host the UI off-box, so the Internet-outage demo would take down OCA's own console.
- **Auth0** would make login depend on an external service during an outage.

What we use instead:

| Need            | Cloud option | Local choice                                                                                                                              |
| --------------- | ------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| Dashboard host  | Vercel       | Server-rendered by the Main Process itself (Jinja2 templates). No separate frontend service.                                              |
| Data + realtime | Supabase     | OCA's own API (§7) + an SSE stream of `ticket_events` for live updates                                                                    |
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

All JS/CSS is vendored into `oca/static/` so the dashboard works with the Internet disconnected.

Why this fits:

- The pages are mostly tables and detail views (tickets, deployments, logs, health checks) with a few actions. That is what HTMX is built for.
- Live agent output is the heart of the demo; HTMX SSE gives that in one attribute, no WebSocket client code.
- The same person owns the API and the UI, so there is no frontend/backend contract to negotiate.
- One fewer container on the GX10, nothing to build for ARM64.

HTML routes (browser only; JSON lives under `/api`, see §7):

```
GET  /                      → redirect to /dash (or /login)
GET  /login, POST /login    → session cookie (itsdangerous-signed); POST /logout
GET  /dash                  → LOCAL AI panel, KPIs, health summary, current tickets; INJECT INCIDENT buttons in dry mode
GET  /tickets               → full page; with HX-Request header returns only the table fragment (auto-refresh 5 s)
GET  /tickets/{id}          → full page; fragments at /tickets/{id}/timeline and /tickets/{id}/gate
GET  /tickets/{id}/stream   → text/event-stream of rendered timeline rows (+ a `status` event that refreshes the gate panel)
POST /tickets/{id}/approve  → returns updated gate-status fragment (also "Override & Approve" for denied actions)
POST /tickets/{id}/deny     → same;  POST /tickets/{id}/run re-launches the agents
GET  /deployments           → table + Restore Backup (POST /deployments/{id}/restore)
GET  /health                → checks table; POST /health/{name}/run, POST /health/{name}/toggle
GET  /logs, GET/POST /settings
```

Vendored in `oca/static/`: htmx 2.0, htmx SSE extension, Alpine.js 3, Pico CSS 2. Chart.js charts are deferred.

---

## 5. Incident lifecycle

A ticket moves through these states. Each transition appends a `status_change` event.

```
 detected ─▶ investigating ─▶ fix_proposed ─▶ sandbox_validated ─▶ reviewed ─▶ gate ─▶ awaiting_approval ─▶ approved ─▶ deploying ─▶ resolved
     │             │                │                 │                │          │            │                              │
     │             │                │                 │                │          │            │                              ├ improved, still breaching ─▶ awaiting_verification ─▶ human confirms ─▶ resolved / rolls back ─▶ rolled_back
     │             │                │                 │                │          │            └ human denies ─▶ denied       └ health worse ─▶ rolled_back
     │             │                │                 │                │          └ policy fails ─▶ denied (human may override)
     │             │                │                 │                └ Guardian NEEDS_TESTING ─▶ Engineer again (with notes)
     │             │                │                 │                └ Guardian REJECT ─▶ back to investigating (max N attempts)
     │             │                │                 ├ not reproduced ─▶ failed (hand-off: production state + diagnosis)
     │             │                │                 └ fix failed ─▶ back to investigating
     │             │                └ Engineer                                              attempts exhausted ─▶ failed
     │             └ Investigator
     └ health script / Jira / Slack / manual
```

`awaiting_approval` is skipped when `settings.require_human_approval` is false (decision D3). Only the orchestrator may set the pipeline states (`investigating` … `gate`, `failed`); the gate, executor and humans set the rest server-side.

| # | State                 | Who                | What happens                                                                              |
| - | --------------------- | ------------------ | ----------------------------------------------------------------------------------------- |
| 1 | `detected`            | Health script / Jira / Slack / human | Check fails → ticket with `CheckResult` and the full status snapshot; or an external issue is imported with its description as the first event. |
| 2 | `investigating`       | Investigator       | Investigates with read-only tools; every tool call streams as a `finding`; posts a `diagnosis`. |
| 3 | `fix_proposed`        | Engineer           | Sandbox prepared and incident reproduced (BEFORE); patch applied on `fix/<ticket>` in the sandbox. |
| 4 | `sandbox_validated`   | Sandbox manager    | AFTER load test + tests after the patch; compared against baseline and SLO.               |
| 5 | `reviewed`            | Guardian           | Risk score + decision (`APPROVE` / `REJECT` / `NEEDS_TESTING`) from server-measured evidence. |
| 6 | `gate`                | Orchestrator → gate | Orchestrator submits `ActionRequest`; gate runs the eight policy checks.                 |
| 7 | `awaiting_approval`   | Human              | Dashboard shows the evidence; operator clicks Approve or Deny.                            |
| 8 | `approved`            | Gate / human       | Backup taken.                                                                             |
| 9 | `deploying`           | Run executor       | Staged rollout against production with monitoring.                                        |
| 10| `resolved`            | Executor verify    | Primary check passes N times; MTTR computed; sandbox torn down.                           |
| 10b| `awaiting_verification` | Executor → human | Deployed and improved but still breaching an SLO; held until the operator confirms or rolls back. |

A human can intervene at any point from the dashboard (approve or override a denied action, deny an approved one, restore a backup, re-run the agents). Every transition also emits an outbound message to any linked external ticket (§4.9).

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
  description   TEXT
  created_at    DATETIME
  updated_at    DATETIME
  resolved_at   DATETIME
  metrics_before JSON          -- baseline snapshot
  metrics_at_detect JSON       -- what tripped the check (NULL for externally reported tickets)
  external_ref  JSON           -- {"system": "jira", "key": "OPS-123", "url": "...", "last_synced_at": ...} or NULL
  attempt       INTEGER        -- orchestrator attempt counter
  current_deployment_id TEXT
  pending_action_id     INTEGER

ticket_events                  -- the timeline; everything the dashboard shows comes from here
  id            INTEGER PK
  ticket_id     TEXT FK
  ts            DATETIME
  actor         TEXT           -- health_check | jira | slack | investigator | engineer | guardian | orchestrator | gate | executor | human | system
  type          TEXT           -- detected | reported | check_failed | finding | diagnosis | patch | sandbox_result | review | action_request | gate_decision | human_decision | run | resolved | rolled_back | status_change | error | external_sync
  payload       JSON

actions                        -- the gate's ledger: every ActionRequest and what the policy engine said
  id            INTEGER PK
  ticket_id     TEXT FK
  action        TEXT
  args          JSON
  requested_by  TEXT
  evidence      JSON
  status        TEXT           -- denied | policy_pass | awaiting_approval | approved | executed | failed
  checks        JSON           -- the eight PolicyCheck rows
  reason        TEXT
  created_at    DATETIME
  decided_at    DATETIME
  decided_by    TEXT           -- gate | human:<user>
  deployment_id TEXT

counters                       -- INC-/DEP-/SBX- sequences
  name          TEXT PK
  next          INTEGER

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

All JSON endpoints are under `/api` on port 8001. The browser uses the HTML routes in §4.11 instead; everything else (Slack, health scripts, agents, external clients) uses these.

| Method | Path                                 | Used by             | Purpose                                                  |
| ------ | ------------------------------------ | ------------------- | -------------------------------------------------------- |
| GET    | `/api/health`                        | everyone            | OCA's own liveness (public)                              |
| GET    | `/api/tickets`                       | clients             | List tickets (`status=`, `service=`, `open=true`)        |
| POST   | `/api/tickets`                       | admin, Jira, Slack, health | Create a ticket                                   |
| GET    | `/api/tickets/{id}`                  | clients, orchestrator | Ticket + full event timeline                           |
| GET    | `/api/tickets/{id}/events`           | clients             | Events after an id                                       |
| POST   | `/api/tickets/{id}/events`           | agents, orchestrator | Append a finding / diagnosis / patch / review (actor forced to the caller's role; the orchestrator may relay an agent's) |
| POST   | `/api/tickets/{id}/status`           | orchestrator        | Pipeline state transitions only                          |
| GET    | `/api/tickets/{id}/stream`           | clients             | SSE of events (`Last-Event-ID` resumes losslessly)       |
| GET    | `/api/tickets/{id}/mttr`             | dashboard           | MTTR card data                                           |
| GET    | `/api/tickets/{id}/report`           | clients             | Incident report as Markdown (written on close, rendered live) |
| POST   | `/api/tickets/{id}/approve`          | human               | Approve the pending action (or override a denial)        |
| POST   | `/api/tickets/{id}/deny`             | human               | Deny the pending action                                  |
| POST   | `/api/tickets/{id}/confirm`, `/rollback` | human           | Resolve or roll back a deployment held for verification |
| GET    | `/api/tickets/{id}/action`           | clients             | The pending/latest action with its policy checks         |
| POST   | `/api/tickets/{id}/run`              | human               | (Re)launch the agent pipeline                            |
| POST   | `/api/actions`                       | orchestrator        | Submit an `ActionRequest` → command check gate           |
| POST   | `/api/sandbox/{id}/prepare`          | orchestrator        | Sandbox up at prod sha + BEFORE load test                |
| POST   | `/api/sandbox/{id}/validate`         | orchestrator        | Rebuild on `fix/<id>` + AFTER load test + tests          |
| POST   | `/api/sandbox/{id}/teardown`         | orchestrator        | Stop the sandbox                                         |
| GET    | `/api/sandbox/{id}`                  | clients             | Latest sandbox run                                       |
| GET    | `/api/deployments`, `/api/deployments/{id}` | clients      | Past deployments with evidence                           |
| POST   | `/api/deployments/{id}/restore`      | human               | Restore from that deployment's backup                    |
| GET    | `/api/checks`                        | clients             | Registered health checks and last results                |
| POST   | `/api/checks/{name}/run`             | human, health       | Run a check now                                          |
| PUT    | `/api/checks/{name}`                 | human               | Enable/disable, change interval                          |
| GET    | `/api/logs`                          | clients             | Ticket events across tickets                             |
| GET/PUT | `/api/settings`                     | human (PUT)         | Approval mode, risk threshold, baseline, thresholds, blocklist, cooldown, time zone, model, Jira/Slack config (secrets masked on GET) |
| POST   | `/api/shutdown`                      | human               | Stop the OCA process (SIGTERM after 0.5 s; lifespan shutdown runs) |
| GET    | `/api/stats/local-ai`                | dashboard           | Cloud requests (0), bytes uploaded (0), ticketing bytes sent, model, hardware |
| POST   | `/api/dev/inject`, `/api/dev/reset`, GET `/api/dev/state` | human | Dry mode only: drive the fake company          |
| GET    | `/api/integrations`, PUT `/api/integrations/{name}`, POST `/api/integrations/{name}/test`, GET `/api/integrations/outbox` | dashboard | Integration status, configure, test connection, outbox queue |

**Agent tool endpoints** — argument shapes are exactly those of `models/tools/catalog.py`. Access is scoped per agent token; every response carries an `evidence_ref` the agents cite.

| Method | Path                            | Args                                   | Investigator | Engineer | Guardian |
| ------ | ------------------------------- | -------------------------------------- | :----------: | :------: | :------: |
| GET    | `/api/tools/logs`               | `service`, `lines` (1–200)             | ✓            |          |          |
| GET    | `/api/tools/containers`         | —                                      | ✓            |          |          |
| GET    | `/api/tools/metrics`            | `service` → recorded `orders_api:*` series + status snapshot | ✓ |     |          |
| GET    | `/api/tools/git`                | `operation=history\|diff`, `ref` (deploy id, sha, `a..b`, `fix/<id>`) | ✓ | ✓ |      |
| GET    | `/api/tools/code`               | `path` (jailed to the repo's readable roots; the Engineer reads the fix branch's view) | ✓ | ✓ |     |
| GET    | `/api/tools/files`              | `path` → files under a readable directory | ✓         | ✓        |          |
| POST   | `/api/tools/sandbox/patch`      | `ticket_id`, `diff`                    |              | ✓        |          |
| POST   | `/api/tools/sandbox/config`     | `ticket_id`, `key`, `value` → server-written `app.env` diff |  | ✓ |          |
| POST   | `/api/tools/sandbox/migration`  | `ticket_id`, `name`, `sql` → next numbered migration |     | ✓        |          |
| POST   | `/api/tools/sandbox/test`       | `ticket_id`                            |              | ✓        |          |
| POST   | `/api/tools/sandbox/loadtest`   | `ticket_id`, `requests?`               |              | ✓        |          |
| GET    | `/api/tools/diff`               | `ticket_id` → diff + deterministic `risk_flags` |     |          | ✓        |
| GET    | `/api/tools/test-results`       | `ticket_id`                            |              |          | ✓        |
| GET    | `/api/tools/sandbox-results`    | `ticket_id`                            |              |          | ✓        |

**Auth:** a bearer token per client type — `OCA_INVESTIGATOR_TOKEN`, `OCA_ENGINEER_TOKEN`, `OCA_GUARDIAN_TOKEN`, `OCA_ORCHESTRATOR_TOKEN`, `OCA_HEALTH_TOKEN`, `OCA_JIRA_TOKEN`, `OCA_SLACK_TOKEN` (dev defaults in `.env.example`). The dashboard uses a signed session cookie and acts as `admin`. Permissions are enforced by the server regardless of what the model asks for; `tests/test_tools_readonly.py` and `tests/test_actions_e2e.py` assert the matrix.

---

## 8. LLM serving and scheduling

**Serving:** Ollama at `http://localhost:11434`; `models/core/llm.py` calls `POST /api/chat` with `model: "nemotron3:33b"` and the tools' JSON schemas; Nemotron returns native `tool_calls` (verified on the GX10: correct typed arguments, ~75 tok/s, 131k context). Verify before wiring agents:

```bash
ollama list
ollama ps
curl http://localhost:11434/api/tags
ollama run nemotron3:33b "Reply with READY."
python3 -m models.main --check --tools      # inference + a synthetic tool round trip
```

**Scheduling:** all three agent roles share one loaded model; role-specific prompts, contexts, and tool sets differentiate them. The initial implementation processes agent requests **sequentially** (Investigator → Engineer → Guardian), which is also the natural order of the pipeline.

An earlier design proposed two concurrent request slots (Investigator + Guardian, then Engineer + Guardian). That remains an **optimization to benchmark**, not a known constraint: with one Ollama-served model the limit is concurrent requests and context size, not VRAM for multiple copies. A bounded semaphore and request queue can be added to the orchestrator once memory use, context sizes, and latency on the GX10 have been measured (roadmap A8).

---

## 9. Fake company (`fakeco/`)

The infrastructure OCA monitors: **Driftwood Coffee Co.**, a miniature company in Docker Compose. It lives in its own git repository (teammates maintain it); OCA points at a clone of it with `OCA_FAKECO_ROOT`. Its own README documents it in full; the parts OCA relies on:

```
loadgen (~30 visits/s) ─▶ frontend nginx :8080 ──/api──▶ orders-api FastAPI :8000 ──▶ postgres :5432 (2M orders)
                                                            │                    └──▶ redis :6379
                                                            ▼
                                                     /metrics ◄── prometheus :9090
                                                     JSON logs ──▶ logs/orders-api.log, logs/frontend.log
git history + state/deployments.jsonl = what changed and when
```

| Primitive                               | OCA uses it for                                                                 |
| --------------------------------------- | ------------------------------------------------------------------------------- |
| `python3 ops/status.py --json`          | the primary detector (`status_check`); SLO 5 % 5xx, 1 s p95, 90 % pool          |
| Prometheus recorded series `orders_api:*` | `metrics_check`, `/api/tools/metrics`                                         |
| `logs/orders-api.log`, `logs/frontend.log` | `/api/tools/logs`, `log_error_parse`                                        |
| `state/deployments.jsonl` + `git diff prev..sha` | `/api/tools/git` (history and the diff of any deployment)              |
| `docker-compose.yml`, application Dockerfiles, `db/init/` | inputs to OCA's own Compose sandbox adapter; sandbox scripts are no longer required |
| `services/orders-api/tests`, `services/loadgen/` | patched-checkout tests in OCA's test container; fixed-size sandbox load tests |
| `git merge --no-ff fix/<ticket> && ops/deploy.sh "<ticket>"` | the `deploy_patch` action (the gate is the only thing allowed to run it) |
| `ops/inject.sh timeout\|loyalty`, `ops/reset.sh` | demo trigger and reset (run by a human in the clone)                    |

Two incidents, both injected as a teammate's merge so they show up in git and the deploy history like any real change:

| Incident  | Deploy                                   | Symptom                                                        | Right fix                                                              |
| --------- | ---------------------------------------- | -------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `timeout` | v1.3.1, `DB_TIMEOUT_MS` 3000 → 100 in `services/orders-api/config/app.env` | `/bestsellers` fails 100 % (`database request timeout`, query `bestsellers_30d`), p95 in seconds; ~25 % errors | revert the value (`set_config`) |
| `loyalty` | v1.4.0, tier lookup filters `orders` by unindexed `customer_email` | everything degrades: pool exhausted, 503s everywhere; ~47 % errors | a **new migration** `services/orders-api/migrations/002_orders_customer_email_idx.sql` with `CREATE INDEX CONCURRENTLY IF NOT EXISTS …` (`deploy_patch`); rolling back would delete a shipped feature |

Measured on a 2-core box: baseline 0 % / p95 ≈ 10 ms. `oca/fakeco/fake_runner.py` reproduces both scenarios deterministically (status, logs, diffs, deploy history, sandbox behaviour, backups) so the whole pipeline runs without Docker.

---

## 10. Demo flow

1. Dashboard shows the company healthy. LOCAL AI panel: 0 cloud requests, 0 bytes uploaded.
2. Press **INJECT INCIDENT** (dry mode) or run `ops/inject.sh timeout` in the driftwood clone. Error rate jumps. **Nobody touches anything.**
3. `status_check` trips → `INC-0001` appears in Current Tickets within one check interval (10 s).
4. Investigator findings stream into the ticket timeline live (SSE): deploy history, the diff of deploy #2, logs, metrics, containers → **ROOT CAUSE** card with confidence and evidence refs.
5. Sandbox spins up at the production sha, reproduces the incident (BEFORE), the Engineer reads `app.env`, applies the one-line revert, re-runs the load test and tests; `validate` records AFTER matching the baseline.
6. Guardian **RELEASE REVIEW**: config-only, risk ≈ 10/100, decision `APPROVE`.
7. The command gate checklist goes green. By default (`require_human_approval` off) the executor deploys immediately; switch it on in Settings to make the ticket wait for an **Approve** click, the one human action in the demo.
8. Backup tag taken; the executor deploys in stages; verify re-runs the health check.
9. Health passes → **INCIDENT RESOLVED**, MTTR card. The linked Jira issue transitions to Done with the MTTR comment, followed by a link to the incident report (§4.10).

To run the demo again after a fix has shipped, reset the company first: `ops/reset.sh && ops/inject.sh timeout`. Re-running `inject.sh` alone is a no-op once its branch is merged (the redeploy only restarts the service), so there is no incident for OCA to find.
10. **Optional closer:** disconnect the GX10 from the Internet and re-run. Everything still works.

**Loyalty variant** (the one that impresses): inject `loyalty`. The right fix is a DB migration, which the Guardian scores ≥ 40; with the default risk threshold of 30 the gate **denies** and the dashboard offers **Override & Approve** — the human-in-the-loop beat. Raise the threshold in Settings for an autonomous run.

**Jira variant**: a support engineer files `OPS-123: "Customers report checkout failing"` in Jira with the `oca` label; within 30 s it appears as a ticket with the Jira key, and the flow continues from step 4.

Measured on a development Mac with `OCA_MODEL=gpt-oss:latest` and the dry runner: detection → awaiting approval in **66 s** (diagnosis 29 s, patch 25 s, review 11 s). On the ASUS Ascent GX10 with `nemotron3:33b`, the real runner and auto-deploy: detection → resolved in **6m 38s** (diagnosis 64 s, fix 203 s, validation 21 s, staged deployment 89 s).

---

## 11. Repository layout

```text
oca/                               # repository root
├── README.md
├── requirements.txt               # single root requirements for oca/ (models/ needs nothing beyond the stdlib)
├── pyproject.toml                 # pytest config (tests/ + models/tests/)
├── .env.example                   # every OCA_* setting
├── models/                        # AI subsystem — stdlib only, never imports oca/
│   ├── config.py                  # ModelConfig from OCA_* env; tiny .env loader
│   ├── core/{llm,agent,tool_registry,state}.py
│   ├── agents/{base,investigator,engineer,guardian}.py
│   ├── prompts/{investigator,engineer,guardian}.txt
│   ├── tools/{catalog,client}.py  # tool declarations + HTTP transport (ToolAPIClient, PlatformClient)
│   ├── orchestrator.py            # handoffs, feedback, retries, ActionRequest submission
│   ├── main.py                    # --check [--tools] | --ticket INC-0042
│   └── tests/                     # unittest: transport, agents, orchestrator (scripted models, fake APIs)
├── oca/                           # platform (FastAPI)
│   ├── main.py                    # app factory, lifespan, python -m oca.main
│   ├── config.py                  # Settings from OCA_* env
│   ├── schemas.py                 # Pydantic contracts (mirrors models/core/state.py for agent outputs)
│   ├── schema.sql, db.py          # SQLite layer
│   ├── events.py                  # per-ticket event bus for SSE
│   ├── auth.py                    # bearer tokens, session cookie, require(roles)
│   ├── allowlist.yaml, gate.py    # command check gate
│   ├── sandbox.py                 # sandbox manager (prepare / patch / validate / teardown)
│   ├── environments/              # OCA-owned Compose lifecycle, company profiles, test image
│   ├── backup.py, executor.py     # git-tag backup; staged rollout + verify + auto-restore
│   ├── pipeline.py                # runs models.orchestrator in-process per ticket
│   ├── fakeco/                    # FakecoRunner protocol, SubprocessRunner (real), FakeRunner (dry), diffcheck, prometheus
│   ├── checks/                    # health checks + scheduler
│   ├── api/                       # /api routes: system, tickets, actions, deployments, sandbox, checks, tools, dev
│   ├── web/, templates/, static/  # dashboard: Jinja2 + vendored htmx/SSE/Alpine/Pico
│   └── integrations/              # Jira/Slack interface, policy filter, outbox (M5)
├── tests/                         # pytest: contracts, db, api, auth matrix, checks, gate, e2e pipeline, dashboard
└── data/                          # runtime (gitignored): oca.db, backups/, sandboxes/
```

Run everything from the repo root: `python3 -m oca.main` (server), `python3 -m models.main --ticket INC-0001` (standalone agents), `python3 -m pytest` (all tests).

---

## 12. Tech stack

| Layer          | Choice                                   | Why                                                                          |
| -------------- | ---------------------------------------- | ---------------------------------------------------------------------------- |
| Hardware       | ASUS Ascent GX10 (GB10, ARM64, 128 GB)   | Competition platform; everything must be ARM-compatible                      |
| Model          | `nemotron3:33b` (local)                  | One shared model across three agent roles; native tool calling via Ollama    |
| Model serving  | Ollama, local `/api/chat`                | Already installed on the box                                                 |
| Agents         | Python 3.12 stdlib only                  | No dependencies; runs anywhere the platform runs                             |
| Backend        | Python 3.12, FastAPI, aiosqlite, pydantic 2 | Fast to build, async, ARM-friendly                                        |
| Infra target   | Docker Compose (multi-arch images)       | driftwood production + sandbox                                               |
| Metrics        | Prometheus + prometheus_client           | Standard, lightweight                                                        |
| Logs           | Structured JSON files written by driftwood | Read directly; no Loki                                                     |
| Dashboard      | HTMX 2 + Jinja2 + Alpine.js, Pico CSS    | Server-rendered by FastAPI; no Node, no build step, works offline (see §4.11) |
| Auth           | Local account, signed session cookie; per-client bearer tokens | Must work offline; no external IdP                    |
| Ticketing      | Jira REST API v2 via `httpx`; Slack Web API | Mirror only; policy-filtered outbound; outbox so it never blocks (§4.9)   |
| Tests          | pytest + anyio (platform), unittest (agents) | 60+ tests, no Docker or GPU needed                                      |

---

## 13. Work split and roadmap

**AI subsystem (`models/`):** Ollama client, bounded agent loop, role prompts, tool clients, agent state, the three agents, orchestration.

**Platform (`oca/`):** API, database, dashboard, health checks, gate, backup, executor, sandbox, runner abstraction, Jira integration, optional Slack. **Driftwood** lives in its own repository (moved out of this tree on 2026-10-10).

Both tracks share the agent output contracts: `models/core/state.py` is the source of truth and `oca/schemas.py` mirrors it (`tests/test_contracts.py`). Tool argument shapes are fixed by `models/tools/catalog.py`.

### Platform milestones

| Stage  | Deliverable                                                                                          | Unblocks           | Status  |
| ------ | ---------------------------------------------------------------------------------------------------- | ------------------ | ------- |
| **M0** | `oca/schemas.py` contracts + `/api` + `/api/tools/*` with per-token scoping                          | AI team, dashboard | Done    |
| **M1** | Driftwood up with loadgen; `ops/inject.sh timeout` visibly raises error rate in Prometheus           | everything         | Done (driftwood repo) |
| **M2** | Health checks + ticket creation + SSE timeline. Inject → ticket appears live on the dashboard.        | A3                 | Done    |
| **M3** | Gate + allowlist + backup + executor + approve/deny/override + auto-restore.                          | A5, A6             | Done    |
| **M4** | OCA-owned Compose sandbox and company profile, with before/after metrics and containerized tests behind `/api/tools/sandbox/*`. | A4 | Implemented (local tests pass; real Docker/GX10 run pending) |
| **M5** | Jira integration: inbound polling → ticket, outbound comments/transitions via outbox, policy filter, Integrations page. | demo (Jira variant) | Planned |
| **M6** | Dashboard: dash, tickets, live ticket detail, deployments, health, logs, settings. Slack (stretch).  | demo               | Done (charts deferred) |
| **M7** | End-to-end with all three agents on the GX10 against the real driftwood clone; offline demo.         | judging            | Next    |

### AI milestones

| Stage  | Deliverable                                                                       | Status                                      |
| ------ | --------------------------------------------------------------------------------- | ------------------------------------------- |
| **A0** | Create `models/` structure and discover installed Ollama models                    | Done                                        |
| **A1** | `nemotron3:33b` inference + native tool calling; `config.py` and `core/llm.py`    | Done (verified live on the GX10)            |
| **A2** | Tool registry and bounded tool-calling agent loop                                  | Done                                        |
| **A3** | Investigator: logs, container state, metrics, git, read-only diagnosis            | Done                                        |
| **A4** | Engineer: sandbox-scoped patching, reproduction, test iteration                    | Done                                        |
| **A5** | Guardian: evidence review, risk evaluation, advisory decision                      | Done                                        |
| **A6** | Orchestrator: handoffs, retries, `ActionRequest` submission, audit trail           | Done                                        |
| **A7** | Integrate with the platform, gate, backup, executor, dashboard                     | Done (dry mode, gpt-oss on a Mac)           |
| **A8** | End-to-end on the GX10 with Nemotron; latency profiling; concurrency benchmark; offline and safety tests | Next               |

Early checks:

- Test on the GX10 early — all images must be ARM64. The `asus` user needs the `docker` group and `pip install -r requirements.txt` in its venv before `OCA_RUNNER=real` works.
- Never allow arbitrary shell commands or production filesystem writes through agent tools (`SubprocessRunner` builds every argv from constants plus validated values).

---

## 14. Decisions

Recorded so nobody re-litigates them by accident. Any can be overturned by the team — update this section if so.

| ID     | Decision                                                                                                                                                                                                           |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **D1** | **Agent tools are HTTP clients.** `models/tools/catalog.py` tools call `oca` `/api/tools/*`. The registry controls which tools an agent is offered; per-agent API tokens control what the server will actually serve.          |
| **D2** | **Guardian is advisory.** Its `APPROVE` never reaches the gate directly. `models/orchestrator.py` builds the `ActionRequest` and posts it to `/api/actions`.                                                       |
| **D3** | **Human approval is a switch, off by default.** `settings.require_human_approval = false`: after the gate's policy checks pass the executor deploys immediately. Turn it on in Settings (or `OCA_REQUIRE_HUMAN_APPROVAL=true`) to make the ticket wait for an Approve click. |
| **D4** | **Single process.** `oca/main.py` runs the orchestrator as an in-process background task. `models/main.py` is a standalone dev entry point only.                                                                  |
| **D5** | **Fully local frontend.** HTMX + Jinja2 + Alpine served by FastAPI; local auth. No Vercel / Supabase / Auth0 (§4.11).                                                                                             |
| **D6** | **Sequential agent execution first.** Concurrency is an A8 benchmark, not a day-one feature (§8).                                                                                                                 |
| **D7** | **Naming.** Top-level `models/` is the agent package; the platform package is `oca/` with contracts in `oca/schemas.py`. One root `requirements.txt`. All settings are `OCA_*` environment variables.               |
| **D8** | **External ticketing is mirror-only and policy-filtered.** Jira/Slack never hold evidence and never receive logs, code, diffs, or metrics. Outbound text is templated from whitelisted fields; `outbound_detail` defaults to `minimal` for Jira Cloud, `summary` for self-hosted. Delivery goes through a retrying outbox so an unreachable Jira never blocks the pipeline. Approve/deny is dashboard-only; Jira comments are never instructions. |
| **D9** | **The platform conforms to the agent contracts.** `models/` is stdlib-only and never imports `oca/`; `oca/schemas.py` mirrors `models/core/state.py` and `/api/tools/*` accepts exactly the shapes in `models/tools/catalog.py`. A test enforces parity. |
| **D10** | **One runner seam.** Everything that touches the company under test goes through `FakecoRunner`: `SubprocessRunner` uses OCA's Compose sandbox adapter plus Driftwood's production ops in its standalone clone (`OCA_FAKECO_ROOT`); `FakeRunner` models both incidents deterministically. Sandbox edits occur only in OCA's worktree; approved deployment may update the company's checkout. |
| **D11** | **Server-measured evidence only.** The sandbox manager records BEFORE/AFTER metrics and test results itself; the gate reloads the sandbox run and the Guardian review from the database and ignores the evidence block's values. Diffs are validated and applied server-side on `fix/<ticket>`. |

---

## 15. Open questions

- Does the competition specifically require Nemotron 3.5 Lightning, and does any installed Ollama tag satisfy it? Don't relabel `nemotron3:33b` until confirmed with organizers.
- Nemotron's JSON final answers: tool calling is verified live; the repair turn with Ollama structured output is the fallback if free-form JSON proves flaky. (A8)
- How many concurrent model requests can Ollama serve reliably on this GX10 at the needed context length? (A8)
- Real-mode timings: `sandbox up` seeds 2M rows (1–2 min) and each `loadtest 2000` takes ~70 s; the Engineer's budgets (`tool_timeout_s` 1500 s, run 3600 s) assume this. Measure on the GX10.
- Loyalty demo: present the gate denial + human override as the human-in-the-loop beat, or raise the risk threshold to 50 for an autonomous run?
- Restore after a migration leaves the index in place; is `ops/reset_db.sh` acceptable as the manual remedy for the demo?
- Jira for the demo: a free Jira Cloud site (`minimal` policy, bytes shown on the panel) or self-hosted Data Center on the GX10 (ARM64 to verify)?
- Which Jira field carries `service`? Component vs. a custom field vs. parsed from the summary.
- Slack: in the demo or stretch only?

## Implementation notes

- **Done (2026-10-10):** `oca/` platform — contracts, SQLite layer, `/api` with per-token scoping, health checks + scheduler, SSE timeline, command gate + allowlist, sandbox manager, backup, staged executor with auto-restore, in-process pipeline, dashboard; `oca/fakeco/` runner seam with a deterministic `FakeRunner` and the real `SubprocessRunner`. `models/` — Ollama client, registry, bounded loop, three agents, tool catalog, orchestrator (with the Guardian evidence audit, total deadline and duplicate-run guard from the parallel orchestrator work), `--ticket` CLI. 64 tests (`python3 -m pytest`), no Docker or GPU required.
- **Verified live:** Nemotron native tool calling on the GX10; the full pipeline on a Mac with `gpt-oss:latest` over the dry runner (`INC-0001` detected → correct diagnosis → one-line fix proven in the sandbox → Guardian APPROVE risk 10 → gate green → awaiting approval in 66 s).
- **Pending:** real-mode run on the GX10 against the standalone driftwood clone (`OCA_RUNNER=real OCA_FAKECO_ROOT=…`; the box needs the `docker` group for `asus` and `pip install -r requirements.txt`); M5 Jira integration (`oca/integrations/`); Chart.js metrics charts; A8 profiling and concurrency benchmark.

### Dashboard appearance update

- Added a light, minimal workspace with sidebar navigation, responsive layouts,
  clearer health and activity summaries, a local AI card, and ticket empty states.
- Kept the existing routes, forms, and HTMX fragment refreshes. Fonts and assets
  remain local. The web interaction tests pass; browser visual review is pending.

### Infrastructure command center update

- Added a dark command-center theme inspired by the provided dashboard reference,
  an SVG infrastructure map with raised nodes, and a system insights panel.
- Animated wires illustrate connections with colors derived from recorded checks,
  model-loaded status, sandbox availability, and the latest active ticket stage.
  Lines do not represent measured network traffic; unknown and stale health is
  shown separately from passing checks.
- Added five-second authenticated map refreshes, a pause control, reduced-motion
  support, and an interrupted-connection state that stops signal animations.
- Seven targeted web/topology tests pass and JavaScript syntax checks pass.
  Visual browser verification remains pending because no browser is connected.
