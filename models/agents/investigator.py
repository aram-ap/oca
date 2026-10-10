"""Read-only incident diagnosis agent."""
from models.agents.base import RoleAgent
from models.core.state import DIAGNOSIS_SCHEMA


class Investigator(RoleAgent):
    ROLE = "investigator"
    OUTPUT_SCHEMA = DIAGNOSIS_SCHEMA
