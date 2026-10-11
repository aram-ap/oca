"""Runs the agent orchestrator in-process, one background task per ticket (D4)."""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from oca.schemas import Actor, EventType, OPEN_STATUSES, Role, TicketEventCreate, TicketStatus

log = logging.getLogger("oca.pipeline")

ENGINEER_TIMEOUT_S = 3600      # sandbox rebuilds and load tests take minutes each
ENGINEER_TOOL_TIMEOUT_S = 1500
AGENT_TIMEOUT_S = 900


class Pipeline:
    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.tasks: dict[str, asyncio.Task] = {}
        self._gate = asyncio.Semaphore(1)  # D6: sequential agent execution

    def launch(self, ticket_id: str) -> asyncio.Task:
        existing = self.tasks.get(ticket_id)
        if existing is not None and not existing.done():
            return existing
        task = asyncio.create_task(self._guarded(ticket_id), name=f"oca-pipeline-{ticket_id}")
        self.tasks[ticket_id] = task
        return task

    async def wait(self, ticket_id: str) -> None:
        task = self.tasks.get(ticket_id)
        if task is not None:
            await task

    def build_orchestrator(self):
        from models.agents import Engineer, Guardian, Investigator
        from models.config import ModelConfig
        from models.core.llm import OllamaClient
        from models.orchestrator import Orchestrator
        from models.tools.client import PlatformClient, ToolAPIClient

        s = self.app.state.settings
        llm = OllamaClient(ModelConfig(model=s.model, ollama_url=s.ollama_url, timeout_s=s.llm_timeout_s,
                                       keep_alive=s.model_keep_alive))
        platform = PlatformClient(token=s.tokens[Role.orchestrator], base_url=s.api_url)
        orchestrator = Orchestrator(platform, max_attempts=s.max_attempts)

        def client(role: Role, timeout: float) -> ToolAPIClient:
            return ToolAPIClient(role=role.value, token=s.tokens[role], base_url=s.api_url, timeout_s=timeout)

        orchestrator.agents = {
            "investigator": Investigator(llm=llm, api_client=client(Role.investigator, s.tool_timeout_s),
                                         on_observation=orchestrator.observer, timeout_s=AGENT_TIMEOUT_S),
            "engineer": Engineer(llm=llm, api_client=client(Role.engineer, ENGINEER_TOOL_TIMEOUT_S),
                                 on_observation=orchestrator.observer, timeout_s=ENGINEER_TIMEOUT_S,
                                 tool_timeout_s=ENGINEER_TOOL_TIMEOUT_S),
            "guardian": Guardian(llm=llm, api_client=client(Role.guardian, s.tool_timeout_s),
                                 on_observation=orchestrator.observer, timeout_s=AGENT_TIMEOUT_S),
        }
        return orchestrator

    async def _guarded(self, ticket_id: str) -> None:
        db = self.app.state.db
        async with self._gate:
            try:
                orchestrator = self.build_orchestrator()
                result = await orchestrator.run_ticket(ticket_id)
                log.info("pipeline for %s finished: %s (%s)", ticket_id, result.status, result.reason)
            except Exception as exc:  # noqa: BLE001 - record and leave the ticket for a human
                log.exception("pipeline for %s crashed", ticket_id)
                try:
                    await db.append_event(ticket_id, TicketEventCreate(
                        actor=Actor.system, type=EventType.error,
                        payload={"where": "pipeline", "error": f"{type(exc).__name__}: {exc}"[:800]}))
                    ticket = await db.get_ticket(ticket_id)
                    if ticket and ticket.status in OPEN_STATUSES and ticket.status not in (
                            TicketStatus.awaiting_approval, TicketStatus.approved, TicketStatus.deploying, TicketStatus.denied):
                        await db.set_status(ticket_id, TicketStatus.failed, Actor.system, note=str(exc)[:200])
                except Exception:  # noqa: BLE001
                    log.exception("could not record pipeline failure for %s", ticket_id)
