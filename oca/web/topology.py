"""Read-only presentation of recorded health and the latest active ticket stage."""

from datetime import datetime, timezone


# The orchestrator moves a ticket sandbox_validated -> reviewed *before* the Guardian runs, so the review
# happens under "reviewed"; "sandbox_validated" itself lasts milliseconds.
REVIEW_STAGES = {"sandbox_validated", "reviewed"}


def topology_context(checks, tickets, local_ai, runner_state):
    enabled = [c for c in checks if c.enabled]
    now = datetime.now(timezone.utc)

    def health(records):
        if any(c.last_result and not c.last_ok for c in records):
            return "danger", "Needs attention"
        if not records or any(not c.last_result for c in records):
            return "pending", "Awaiting checks"
        for check in records:
            last = check.last_run
            if last is None or (now - last.replace(tzinfo=last.tzinfo or timezone.utc)).total_seconds() > max(check.interval_s * 2, 60):
                return "pending", "Checks outdated"
        return "healthy", "Checks passing"

    overall, overall_label = health(enabled)
    metrics = [c for c in enabled if c.name in {"metrics_check", "status_check"}]
    observability, obs_label = health(metrics)
    active = next((t for t in tickets if t.status.value in {
        "detected", "investigating", "fix_proposed", "sandbox_validated", "reviewed",
        "gate", "awaiting_approval", "approved", "deploying"}), None)
    stage = active.status.value if active else None
    loaded = local_ai["loaded"]
    sandbox_up = runner_state.get("sandbox", {}).get("up") if runner_state else None

    def node(key, title, subtitle, x, y, state="idle", label="Idle", href="/health"):
        return {"key": key, "title": title, "subtitle": subtitle, "x": x, "y": y,
                "state": state, "label": label, "href": href}

    nodes = [
        node("model", "Local Nemotron", local_ai["model"], 460, 55,
             "healthy" if loaded else "idle" if loaded is False else "pending",
             "Model loaded" if loaded else "Model idle" if loaded is False else "Status unavailable", "/settings"),
        node("investigator", "Investigator", "Evidence & diagnosis", 170, 205,
             "active" if stage == "investigating" else "idle", "Investigating" if stage == "investigating" else "Idle", "/tickets"),
        node("engineer", "Engineer", "Sandbox fixes & tests", 460, 205,
             "active" if stage == "fix_proposed" else "idle", "Building fix" if stage == "fix_proposed" else "Idle", "/tickets"),
        node("guardian", "Guardian", "Evidence & risk review", 750, 205,
             "active" if stage in REVIEW_STAGES else "idle", "Reviewing" if stage in REVIEW_STAGES else "Idle", "/tickets"),
        node("orchestrator", "Orchestrator", "Incident coordination", 460, 345,
             "active" if active else "idle", "Awaiting approval" if stage == "awaiting_approval" else "Incident in progress" if active else "No active ticket", "/tickets"),
        node("database", "Incident state", "SQLite · local record", 170, 465, "healthy", "Responding", "/logs"),
        node("api", "Main Process API", "Policy & tool access", 460, 465, "healthy", "Responding", "/settings"),
        node("production", "Production", "Driftwood services", 170, 610, overall, overall_label),
        node("sandbox", "Sandbox", "Isolated validation", 460, 610,
             "active" if sandbox_up else "idle" if sandbox_up is False else "pending",
             "Running" if sandbox_up else "Stopped" if sandbox_up is False else "Status unavailable"),
        node("observability", "Observability", "Logs & Prometheus", 750, 610, observability, obs_label),
    ]
    states = {n["key"]: n["state"] for n in nodes}
    # The wires illustrate component relationships, not measured packet traffic.
    links = [
        ("model", "investigator", "M550 143 V170 H260 V205"),
        ("model", "engineer", "M550 143 V205"),
        ("model", "guardian", "M550 143 V170 H840 V205"),
        ("investigator", "orchestrator", "M260 293 V320 H550 V345"),
        ("engineer", "orchestrator", "M550 293 V345"),
        ("guardian", "orchestrator", "M840 293 V320 H550 V345"),
        ("orchestrator", "api", "M550 433 V465"),
        ("api", "database", "M460 509 H350"),
        ("api", "production", "M550 553 V580 H260 V610"),
        ("api", "sandbox", "M550 553 V610"),
        ("api", "observability", "M550 553 V580 H840 V610"),
    ]
    wires = []
    for source, target, path in links:
        source_state, target_state = states[source], states[target]
        ends = {source_state, target_state}
        # Nothing travels along a wire into an idle component: the orchestrator is active for the whole
        # ticket, so "active if either end is active" lit the Engineer and Guardian wires while both sat idle.
        state = ("danger" if "danger" in ends else "pending" if "pending" in ends else "idle" if "idle" in ends
                 else "active" if "active" in ends else "healthy")
        wires.append({"path": path, "state": state})
    return {"nodes": nodes, "wires": wires, "overall": overall, "overall_label": overall_label,
            "active_ticket": active, "checked_at": now.strftime("%H:%M:%S UTC"),
            "passing": sum(bool(c.last_result and c.last_ok) for c in enabled),
            "enabled": len(enabled), "failing": sum(bool(c.last_result and not c.last_ok) for c in enabled)}
