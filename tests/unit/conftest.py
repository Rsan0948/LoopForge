"""Unit harness clients share a test-owned lease, never the live model lock."""

from pathlib import Path

import pytest

from loopforge.adapters.model_lease import ModelLease
from loopforge.entrypoints import campaign


@pytest.fixture(autouse=True)
def isolated_campaign_model_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(campaign, "ModelLease", lambda: ModelLease(tmp_path / "model.lock"))
