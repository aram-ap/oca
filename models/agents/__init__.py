"""Three role-specific agents sharing one local model transport."""
from models.agents.engineer import Engineer
from models.agents.guardian import Guardian
from models.agents.investigator import Investigator

__all__ = ["Investigator", "Engineer", "Guardian"]
