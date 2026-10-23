"""Scripted campaign integrations use a test-owned model lease."""

from pathlib import Path

import pytest

from loopforge.adapters.model_lease import ModelLease
from loopforge.entrypoints import campaign


@pytest.fixture(autouse=True)
def isolated_campaign_model_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(campaign, "ModelLease", lambda: ModelLease(tmp_path / "model.lock"))
