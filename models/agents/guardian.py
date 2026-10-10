"""Read-only release reviewer; approval is advisory."""
from models.agents.base import RoleAgent
from models.core.state import REVIEW_SCHEMA


class Guardian(RoleAgent):
    ROLE = "guardian"
    OUTPUT_SCHEMA = REVIEW_SCHEMA
