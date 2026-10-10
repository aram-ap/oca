"""Sandbox-only patch and validation agent."""
from models.agents.base import RoleAgent
from models.core.state import PATCH_SCHEMA


class Engineer(RoleAgent):
    ROLE = "engineer"
    OUTPUT_SCHEMA = PATCH_SCHEMA
