"""Sandbox-only patch and validation agent."""
from models.agents.base import RoleAgent
from models.core.state import PATCH_SCHEMA


class Engineer(RoleAgent):
    ROLE = "engineer"
    OUTPUT_SCHEMA = PATCH_SCHEMA
    WRITE_TOOLS = ("set_config", "create_migration", "apply_patch")
    REQUIRED_TOOLS = WRITE_TOOLS
    MUTATING_TOOLS = WRITE_TOOLS
