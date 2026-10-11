"""Fixed endpoint catalog. Argument contracts must be matched by oce/schemas.py."""

from models.core.tool_registry import Tool, ToolError, ToolRegistry, object_schema
from models.tools.client import ToolAPIError

TEXT = {"type": "string", "minLength": 1}
SERVICE = {"type": "string", "enum": ["orders-api", "frontend", "postgres", "redis"]}
READERS = frozenset({"investigator", "engineer"})
INV = frozenset({"investigator"})
ENG = frozenset({"engineer"})
GUARD = frozenset({"guardian"})

CATALOG = [
    ("get_logs", "Read bounded service logs.", INV, "GET", "/api/tools/logs",
     object_schema({"service": SERVICE, "lines": {"type": "integer", "minimum": 1, "maximum": 200}}, ["service"])),
    ("get_containers", "Read container status.", INV, "GET", "/api/tools/containers", object_schema({})),
    ("get_metrics", "Read Prometheus metrics for a service.", INV, "GET", "/api/tools/metrics", object_schema({"service": SERVICE})),
    ("get_code", "Read a repository-relative file; server enforces allowed roots.", READERS,
     "GET", "/api/tools/code", object_schema({"path": TEXT})),
    ("get_git", "Read deployment history or a diff, never change git state.", READERS,
     "GET", "/api/tools/git", object_schema({"operation": {"type": "string", "enum": ["history", "diff"]},
                                            "ref": TEXT}, ["operation"])),
    ("list_files", "List files under a repository directory (get_code reads one file; it cannot list).", READERS,
     "GET", "/api/tools/files", object_schema({"path": TEXT})),
    ("set_config", "Set KEY=value in services/orders-api/config/app.env on the fix branch; the server writes and "
                   "applies the diff. Preferred for any config fix.", ENG,
     "POST", "/api/tools/sandbox/config", object_schema({"ticket_id": TEXT, "key": TEXT, "value": {"type": "integer"}})),
    ("create_migration", "Create the next numbered services/orders-api/migrations/NNN_<name>.sql with this SQL on the "
                         "fix branch; the server writes and applies the diff. Preferred for index fixes.", ENG,
     "POST", "/api/tools/sandbox/migration", object_schema({"ticket_id": TEXT, "name": TEXT, "sql": TEXT})),
    ("apply_patch", "Apply a unified diff only in this ticket's sandbox worktree (for code changes that set_config "
                    "and create_migration cannot express).", ENG,
     "POST", "/api/tools/sandbox/patch", object_schema({"ticket_id": TEXT, "diff": TEXT})),
    ("run_tests", "Run the server's allowlisted sandbox test suite; no shell commands.", ENG,
     "POST", "/api/tools/sandbox/test", object_schema({"ticket_id": TEXT})),
    ("run_loadtest", "Run a sandbox load test; first reproduce, then test the fix.", ENG,
     "POST", "/api/tools/sandbox/loadtest", object_schema({"ticket_id": TEXT,
     "requests": {"type": "integer", "minimum": 1, "maximum": 10000}}, ["ticket_id"])),
    ("get_diff", "Read the proposed patch diff.", GUARD, "GET", "/api/tools/diff", object_schema({"ticket_id": TEXT})),
    ("get_test_results", "Read sandbox test evidence.", GUARD, "GET", "/api/tools/test-results", object_schema({"ticket_id": TEXT})),
    ("get_sandbox_results", "Read reproduction and before/after load-test evidence.", GUARD,
     "GET", "/api/tools/sandbox-results", object_schema({"ticket_id": TEXT})),
]


def build_registry(client) -> ToolRegistry:
    tools = []
    for name, description, roles, method, path, schema in CATALOG:
        if client.role not in roles:
            continue

        def bind(method, path):
            async def handler(**arguments):
                try:
                    return await client.request(method, path, arguments)
                except ToolAPIError as exc:
                    # A 4xx is the server rejecting *this* request (unknown path, bad ref, conflict): the model
                    # should see it and try something else. A failed GET is also safe to show, whatever the
                    # status: nothing was written. A 5xx or transport failure on a POST still aborts, because
                    # the write may or may not have happened and must not be retried blindly.
                    if exc.status is not None and (400 <= exc.status < 500 or method == "GET"):
                        raise ToolError(f"HTTP {exc.status}: {exc.body[:300] or 'request failed'}") from exc
                    raise
            return handler

        tools.append(Tool(name, description, schema, roles, bind(method, path)))
    return ToolRegistry(tools)
