"""Read-only presentation of recorded health and the latest active ticket stage."""

from datetime import datetime, timezone


def topology_context(checks, tickets, local_ai, runner_state, settings=None):
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
    dry = settings is None or settings.runner == "dry"
    endpoint_checks = [c for c in enabled if c.name.startswith("endpoint_check:")]
    connection_state, connection_label = health(endpoint_checks)
    if not dry and connection_state == "healthy":
        checked_services = {getattr(c, "service", None) for c in endpoint_checks}
        if set(settings.service_urls) - checked_services:
            connection_state, connection_label = "pending", "Awaiting endpoint checks"
    if dry:
        connection_state, connection_label = "idle", "Simulated connection"
    elif connection_state == "healthy":
        connection_label = "Endpoints responding"
    elif connection_state == "danger":
        connection_label = "Endpoint check failed"
    services = []
    for key, title, subtitle, x, y in [
        ("frontend", "Storefront", "nginx · web", 1010, 210),
        ("orders-api", "Orders API", "FastAPI · app", 1250, 350),
        ("postgres", "PostgreSQL", "Company database", 1020, 705),
        ("redis", "Redis", "Cache & sessions", 1260, 800),
    ]:
        service_checks = [c for c in enabled if getattr(c, "service", None) == key]
        state, label = health(service_checks)
        if not service_checks:
            label = "No direct health check"
        services.append({"key": key, "title": title, "subtitle": subtitle, "x": x, "y": y,
                         "state": state, "label": label})

    def node(key, title, subtitle, x, y, state="idle", label="Idle", href="/health"):
        return {"key": key, "title": title, "subtitle": subtitle, "x": x, "y": y,
                "state": state, "label": label, "href": href,
                "height": {"model": 110, "orchestrator": 128, "database": 100,
                           "postgres": 100, "redis": 64}.get(key, 80)}

    nodes = [
        node("model", "Local Nemotron", local_ai["model"], 140, 90,
             "healthy" if loaded else "idle" if loaded is False else "pending",
             "Model loaded" if loaded else "Model idle" if loaded is False else "Status unavailable", "/settings"),
        node("investigator", "Investigator", "Evidence & diagnosis", 160, 300,
             "active" if stage == "investigating" else "idle", "Investigating" if stage == "investigating" else "Idle", "/tickets"),
        node("engineer", "Engineer", "Sandbox fixes & tests", 480, 155,
             "active" if stage == "fix_proposed" else "idle", "Building fix" if stage == "fix_proposed" else "Idle", "/tickets"),
        node("guardian", "Guardian", "Evidence & risk review", 755, 310,
             "active" if stage == "sandbox_validated" else "idle", "Review stage" if stage == "sandbox_validated" else "Idle", "/tickets"),
        node("orchestrator", "OCA Hub", "Orchestrator", 485, 450,
             "active" if active else "idle", "Awaiting approval" if stage == "awaiting_approval" else "Incident in progress" if active else "No active ticket", "/tickets"),
        node("database", "Incident state", "SQLite · local record", 140, 540, "healthy", "Responding", "/logs"),
        node("api", "Access gateway", "Main Process API", 480, 730, "healthy", "Responding", "/settings"),
        node("production", "Production", "Driftwood services", 1130, 560, overall, overall_label),
        node("sandbox", "Sandbox", "Isolated validation", 755, 625,
             "active" if sandbox_up else "idle" if sandbox_up is False else "pending",
             "Running" if sandbox_up else "Stopped" if sandbox_up is False else "Status unavailable"),
        node("observability", "Observability", "Logs & Prometheus", 170, 785, observability, obs_label),
    ]
    nodes.extend(node(s["key"], s["title"], s["subtitle"], s["x"], s["y"],
                      s["state"], s["label"]) for s in services)
    states = {n["key"]: n["state"] for n in nodes}
    heights = {n["key"]: n["height"] for n in nodes}
    # The wires illustrate component relationships, not measured packet traffic.
    links = [("orchestrator", key) for key in (
        "investigator", "engineer", "guardian", "model", "api", "database", "sandbox", "observability")]
    links += [("api", "production"), ("production", "frontend"),
              ("frontend", "orders-api"), ("orders-api", "postgres"), ("orders-api", "redis")]
    positions = {n["key"]: (n["x"] + 90, n["y"] + 44) for n in nodes}
    wires = []
    for source, target in links:
        source_state, target_state = states[source], states[target]
        state = "danger" if "danger" in {source_state, target_state} else "pending" if "pending" in {source_state, target_state} else "active" if "active" in {source_state, target_state} else "healthy" if source_state == target_state == "healthy" else "idle"
        sx, sy = positions[source]
        tx, ty = positions[target]
        dx, dy = tx - sx, ty - sy
        # Attach to facing roof edges. Smooth arcs suit the campus view rather
        # than reproducing the architecture drawing's rectilinear routes.
        if abs(dx) > abs(dy):
            offset = 86 if dx > 0 else -86
            sx, tx = sx + offset, tx - offset
        else:
            offset = 40 if dy > 0 else -40
            sy, ty = sy + offset, ty - offset
        bend = (tx - sx) * .45
        path = f"M{sx} {sy} C{sx + bend:g} {sy} {tx - bend:g} {ty} {tx} {ty}"
        wires.append({"path": path, "state": state, "bridge": target == "production", "ports": [
            {"x": sx, "y": sy, "drop": 148 - heights[source]},
            {"x": tx, "y": ty, "drop": 148 - heights[target]},
        ]})
    return {"nodes": nodes, "wires": wires, "overall": overall, "overall_label": overall_label,
            "company": {"name": "Driftwood Coffee Co.", "state": connection_state,
                        "label": connection_label, "dry": dry,
                        "root": str(settings.fakeco_root) if settings else "Simulated environment",
                        "services": services},
            "active_ticket": active, "checked_at": now.strftime("%H:%M:%S UTC"),
            "passing": sum(bool(c.last_result and c.last_ok) for c in enabled),
            "enabled": len(enabled), "failing": sum(bool(c.last_result and not c.last_ok) for c in enabled)}
