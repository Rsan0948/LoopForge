"""Loaded release identity; cosmetic Python edits do not authorize retries."""

import ast
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from loopforge.adapters.ollama_profiles import profile_for


class InstalledModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_version: str
    template_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    parameters: str


def release_manifest() -> dict[str, object]:
    root = Path(__file__).parents[1]
    sources = {}
    for path in sorted(root.rglob("*.py")):
        content = path.read_text()
        tree = ast.dump(ast.parse(content), include_attributes=False)
        sources[str(path.relative_to(root))] = hashlib.sha256(tree.encode()).hexdigest()
    workflows = root.parents[1] / "integrations/n8n"
    templates = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(workflows.glob("*.json"))
    }
    identity_path = root.parents[1] / ".loopforge/campaign/release-model.json"
    installed = (
        InstalledModel.model_validate_json(identity_path.read_text()).model_dump()
        if identity_path.exists()
        else None
    )
    body = {
        "semantic_sources": sources,
        "workflow_templates": templates,
        "installed_model": installed,
        "effective_profiles": {
            "coding_baseline": asdict(profile_for("coding")),
            "coding_candidate": asdict(profile_for("coding", candidate=True)),
            "review_generic": asdict(replace(profile_for("review"), output_tokens=2048)),
            "review_patch": asdict(profile_for("review")),
            "reporter": asdict(profile_for("reporter")),
        },
        "model_identity_basis": (
            "operator-captured provider metadata at installation; not live residency"
        ),
    }
    return {
        **body,
        "fingerprint": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
    }
