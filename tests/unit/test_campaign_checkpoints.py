"""A retry resumes from the best verified candidate, not a later regression."""

from pathlib import Path

import pytest

from loopforge.adapters import ollama_model
from loopforge.adapters.git_workspace import GitWorkspace
from loopforge.entrypoints import campaign_coding as coding
from loopforge.entrypoints import campaign_execution as execution
from loopforge.entrypoints.campaign import CampaignHarness
from loopforge.entrypoints.campaign_checkpoints import CheckpointStore
from loopforge.entrypoints.campaign_coding import patch_identity
from loopforge.entrypoints.campaign_models import CampaignConfig, CodingCommand, OperatorCommand
from tests.unit.test_campaign_coding import workspace
from tests.unit.worker_trajectory import (
    FEATURES,
    GAME,
    Calls,
    LocalChecks,
    NoSleep,
    Trajectory,
    Turn,
    add_feature,
    build_task,
    build_workspace,
    call,
    marker,
)


def _verified(store: CheckpointStore, ws: GitWorkspace, score: float) -> str:
    identity = patch_identity(ws)
    store.record(ws, identity, score)
    return identity


def test_a_regressed_candidate_is_put_back_to_the_best_one(tmp_path: Path) -> None:
    ws = workspace(tmp_path / "repo")
    store = CheckpointStore(tmp_path / "checkpoints" / "task.json")
    (ws.root / "src/add.py").write_text("def add(a,b): return a+b\n")
    _verified(store, ws, 0.5)
    launcher = ws.root / "src" / "run.sh"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    (ws.root / "docs/api.md").unlink()
    best = _verified(store, ws, 0.7)
    # A later attempt rewrites everything and scores lower.
    (ws.root / "src/add.py").write_text("broken\n")
    launcher.unlink()
    (ws.root / "docs/api.md").write_text("Addition returns zero.\n")
    (ws.root / "src/extra.py").write_text("x = 1\n")
    _verified(store, ws, 0.3)

    restored = store.restore_best(ws)

    assert restored is not None
    assert restored.score == 0.7
    assert patch_identity(ws) == best
    assert (ws.root / "src/add.py").read_text() == "def add(a,b): return a+b\n"
    assert launcher.stat().st_mode & 0o777 == 0o755
    assert not (ws.root / "docs/api.md").exists()
    assert not (ws.root / "src/extra.py").exists()
    assert store.restore_best(ws) is None  # already the best


def test_the_best_candidate_is_never_replaced(tmp_path: Path) -> None:
    ws = workspace(tmp_path / "repo")
    store = CheckpointStore(tmp_path / "task.json")
    (ws.root / "src/add.py").write_text("def add(a,b): return a+b\n")
    _verified(store, ws, 0.7)
    assert store.restore_best(ws) is None
    (ws.root / "src/add.py").write_text("def add(a,b): return b+a\n")
    _verified(store, ws, 0.7)  # a tie is not better
    assert store.restore_best(ws) is None


def test_an_unverified_change_since_the_last_verification_is_left_alone(tmp_path: Path) -> None:
    ws = workspace(tmp_path / "repo")
    store = CheckpointStore(tmp_path / "task.json")
    (ws.root / "src/add.py").write_text("def add(a,b): return a+b\n")
    _verified(store, ws, 0.7)
    (ws.root / "src/add.py").write_text("worse\n")
    _verified(store, ws, 0.2)
    (ws.root / "src/add.py").write_text("an operator edit\n")
    assert store.restore_best(ws) is None
    assert (ws.root / "src/add.py").read_text() == "an operator edit\n"


def test_no_checkpoint_means_no_restore(tmp_path: Path) -> None:
    assert CheckpointStore(tmp_path / "none.json").restore_best(workspace(tmp_path)) is None


def test_a_retry_through_the_coordinator_starts_from_the_best_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coding, "ContainerSandbox", LocalChecks)
    monkeypatch.setattr(coding, "SystemSleeper", NoSleep)
    monkeypatch.setattr(execution, "preflight", lambda _task: None)  # pyright: ignore[reportUnknownLambdaType,reportUnknownArgumentType]
    source = build_workspace(tmp_path / "source")
    features = FEATURES[:6]
    harness = CampaignHarness(tmp_path / "campaign")
    config = CampaignConfig(
        repository=str(source.root),
        project="snake",
        revision=source.base_revision,
        scope=["app"],
        rounds=1,
        inference_profile="qwen-native-v1",
    )
    campaign = harness.create(config)
    task = harness.coding.enqueue(
        campaign, "snake_task", build_task(features).model_copy(update={"kind": "bugfix"})
    )
    seen: list[str] = []

    def look(root: Path) -> Calls:
        seen.append((root / GAME).read_text())
        return call("workspace_status")

    first: list[Turn] = [call("read_file", path=GAME)]
    for name, _ in features[:3]:
        first += [add_feature(name), call("check_acceptance_gameplay")]
    # The attempt then rewrites the file and loses two features, and stops making progress.
    first.append(call("write_file", path=GAME, content="// Snake\n" + marker(features[0][0])))
    attempts = iter([Trajectory(first), Trajectory([look])])
    real = ollama_model.OllamaModel
    root = harness.coding.directory / "coding" / task

    def factory(**kwargs: object) -> ollama_model.OllamaModel:
        trajectory = next(attempts)
        return real(**{**kwargs, "transport": trajectory.transport(root)})  # type: ignore[arg-type]

    monkeypatch.setattr(coding, "OllamaModel", factory)
    harness.acquire_service()

    def attempt() -> None:
        ticked = harness.tick(campaign)
        assert ticked["accepted"] is True, ticked
        assert harness.worker is not None
        harness.worker.join()

    attempt()
    assert marker(features[2][0]) not in (root / GAME).read_text()  # the regression
    harness.coding.command(campaign, task, CodingCommand(command="retry", evidence="continue"))
    harness.command(campaign, OperatorCommand(command="resume", evidence="retry"))
    attempt()

    assert all(marker(name) in seen[0] for name, _ in features[:3])
    harness.close()
