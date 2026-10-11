# Local model subsystem

The first implemented milestone is configuration and a non-streaming async
Ollama client. Agent loops, tools, role prompts, and orchestration remain planned.
Python 3.12+ is supported; this milestone uses only the standard library.

From the repository root, on the GX10 with Ollama running and the model installed:

```bash
python3 -m models.main --check
python3 -m models.main --check --tools
```

The second command checks a complete tool-call round trip with a fixed synthetic
health result. It does not access fakeco or run infrastructure commands. A failure
does not establish that a different model tag is required; inspect the model's
actual behavior before proceeding to the agent loop.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `OCE_MODEL` | `nemotron3:33b` | Installed Ollama model tag |
| `OCE_OLLAMA_URL` | `http://localhost:11434` | HTTP loopback service address |
| `OCE_LLM_TIMEOUT_S` | `120` | Socket timeout in seconds |
| `OCE_MODEL_KEEP_ALIVE` | `5m` | How long Ollama keeps the model loaded |

The client ignores system proxies, rejects redirects and non-loopback URLs,
preserves tool calls, and never executes tools. HTTP failures and malformed
responses raise `OllamaError`. There are no automatic retries. Socket timeouts
are not a total agent deadline; the future agent loop must enforce its own budget.

API references: [Ollama chat](https://docs.ollama.com/api/chat) and
[tool calling](https://docs.ollama.com/capabilities/tool-calling).

Run the transport checks without Ollama:

```bash
python3 -m unittest discover -s models/tests -v
```

## Agent implementation checkpoint

The three role classes are now implemented in `models/agents/`: `Investigator`,
`Engineer`, and `Guardian`. Each loads its own prompt and uses the shared bounded
loop, validated tool registry, and fresh per-run history. The loop returns an
`AgentResult` with structured output and tool observations. Guardian's decision
is advisory; none of these agents can submit actions or deploy.

For live use, configure `OCE_API_URL` (default `http://localhost:8001`, chosen to
avoid fakeco's port 8000), `OCE_TOOL_TIMEOUT_S` (default 30), and a distinct scoped
token for each role: `OCE_INVESTIGATOR_TOKEN`, `OCE_ENGINEER_TOKEN`,
`OCE_GUARDIAN_TOKEN`. There are no default tokens. Run limits default to 12 model
turns, 300 seconds total, 30 seconds per tool, and 20,000 characters per observation.
They can be passed as constructor arguments.

```python
from models.agents import Investigator

# Inside an async function; Ollama and the Main Process API must be running.
result = await Investigator().run({
    "ticket_id": "INC-0042",
    "service": "orders-api",
    "title": "Database timeouts after deployment",
})
print(result.output)
```

The HTTP tool catalog follows the README's endpoint paths. Its argument shapes
are provisional until shared `oce/schemas.py` and the Main Process are implemented:
logs use `service` and optional `lines`; metrics use `service`; code uses `path`;
git uses `operation` (`history` or `diff`) and optional `ref`; sandbox tools use
`ticket_id` (patch also uses `diff`; loadtest accepts optional `requests`);
review tools use `ticket_id`. Tests always select the server's configured suite,
never a model-supplied shell command. The Main Process must enforce role tokens,
filesystem roots, sandbox isolation, and policy. These client checks do not
replace that server enforcement. Tools return JSON evidence from the server.

Output validation currently lives in `models/core/state.py`: diagnosis contains
confidence from 0 to 1; Engineer returns patch/reproduction/test/before/after
evidence; Guardian returns a decision and integer risk score from 0 to 100.
These are agent-side contracts, not a replacement for the planned platform schemas.

Transport failures stop the run without retrying writes. Cancelling an agent's
deadline does not undo a request already accepted by the server or stop its worker
thread immediately; the server must enforce operation deadlines and safe recovery.
Prompts direct evidence gathering and sandbox proof; deterministic production
approval still belongs to the future command gate, not these model outputs.

Orchestration, ticket event persistence, the Main Process API, and live GX10/fakeco
integration remain pending. Tests use scripted models and simulated tool APIs;
they do not establish Nemotron's real reasoning or tool-calling performance.

## Orchestrator implementation checkpoint

`models/orchestrator.py` now coordinates Investigator -> Engineer -> Guardian
sequentially. `NEEDS_TESTING` returns to Engineer with review feedback; `REJECT`
or failed reproduction/validation returns to Investigator. The default budget is
three Engineer/review attempts and 1,200 seconds total. Exhaustion or agent/output
errors return an escalated result for human intervention. Cancellation/timeouts
and uncertain API operations raise to the caller; they are never automatically
replayed. The Main Process must handle those exceptions and recovery.

Before advancing, the orchestrator posts local timeline events. It validates role
outputs, then checks Guardian's retrieved diff/test/sandbox evidence before building
the README's `deploy_patch` ActionRequest. Submission calls `/api/actions` exactly
once per successful run and returns the gate response. It never approves, deploys,
marks an incident resolved, or contacts Jira/Slack. Those are platform responsibilities.

`models/platform_client.py` uses the distinct `OCE_ORCHESTRATOR_TOKEN` and only
exposes ticket fetch, event append, and action submission. Agent tokens remain
separate. With the Main Process implemented and all four tokens configured:

```bash
python3 -m models.main --ticket INC-0042 --max-attempts 3
```

The CLI prints a JSON pipeline result. Exit codes are 0 for a submitted request
(inspect `gate_response` for the actual policy decision), 2 for escalation,
and 1 for a runtime failure. `--check --tools` still runs the model smoke check.

The platform handoff contracts need agreement with the platform teammate:

- `GET /api/tickets/{id}` returns a ticket object with `id` and `status: detected`.
  The entire ticket, including baseline metrics/description, is passed to agents.
- `POST /api/tickets/{id}/events` accepts `actor`, `type`, and `payload`. Lifecycle
  transitions use `type: run` with `payload.status`; role events carry `output`,
  `steps`, and `observations`. Escalation is a run event with `phase: escalated`
  and `requires_human: true`, not a new ticket-status enum.
- Guardian's `get_diff` returns `patch_ref` and `diff`; `get_test_results` returns
  `patch_ref`, `sandbox_run_id`, and boolean `passed`; `get_sandbox_results`
  returns `patch_ref`, `sandbox_run_id`, and booleans `reproduced` and `passed`.
  All references must agree with the Engineer's patch. These fields must come
  from server-recorded evidence, not model-generated narratives.
- `/api/actions` independently verifies the referenced evidence, approval mode,
  risk threshold, and policy; it returns a JSON gate response. Server-side ticket
  claiming/idempotency is needed for multiple orchestrators/processes. The local
  duplicate-run guard only covers concurrent runs in one orchestrator instance;
  automatic resume/recovery is not implemented.

Test the orchestration without Ollama or a platform server:

```bash
python3 -m unittest discover -s models/tests -p test_orchestrator.py -v
```

This includes a complete run through the actual agent loops with scripted model
responses and simulated platform tools, plus retries, escalation, evidence mismatch,
audit failures, deadlines, and uncertain submissions. Live agent reasoning and
platform integration remain pending; the earlier checkpoint notes describe the
state before this addition.
