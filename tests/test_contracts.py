"""oca.schemas agent contracts must match models/core/state.py field for field."""

from models.core.state import DIAGNOSIS_SCHEMA, PATCH_SCHEMA, REVIEW_SCHEMA
from oca.schemas import Diagnosis, FixProposal, GuardianDecision, Review


def _compare(model, schema):
    js = model.model_json_schema()
    assert set(js["properties"]) == set(schema["properties"])
    assert set(js.get("required", [])) == set(schema["required"])
    assert js.get("additionalProperties") is False


def test_diagnosis_matches():
    _compare(Diagnosis, DIAGNOSIS_SCHEMA)


def test_fix_proposal_matches():
    _compare(FixProposal, PATCH_SCHEMA)


def test_review_matches():
    _compare(Review, REVIEW_SCHEMA)
    assert {d.value for d in GuardianDecision} == set(REVIEW_SCHEMA["properties"]["decision"]["enum"])
    risk = REVIEW_SCHEMA["properties"]["risk_score"]
    assert (risk["minimum"], risk["maximum"]) == (0, 100)


def test_models_package_stays_stdlib_only():
    """The agent package must not import the platform or third-party libraries."""
    import pathlib
    import re

    bad = []
    for path in pathlib.Path("models").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for mod in re.findall(r"^\s*(?:from|import)\s+([\w.]+)", text, re.M):
            root = mod.split(".")[0]
            if root in {"oca", "pydantic", "fastapi", "httpx", "aiosqlite"}:
                bad.append(f"{path}: {mod}")
    assert not bad, bad
