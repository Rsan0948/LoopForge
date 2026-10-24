"""Whole worker runs, scripted and offline, asserting the outcome a run *should* have.

Each case is a realistic shape of worker behaviour taken from live runs, driven under
both worker profiles. ``known_gap`` marks a case that today's gates end early, for the
profiles named, as a strict xfail: when a fix lands the case passes, the strict xfail
fails, and the marker must be removed in the same change.
"""

from pathlib import Path

import pytest

from loopforge.domain.types import RunStatus
from tests.unit.worker_trajectory import (
    FEATURES,
    GAME,
    Outcome,
    Trajectory,
    Turn,
    add_feature,
    build_task,
    call,
    drive,
    marker,
)

PROFILES = ("qwen-native-v1", "qwen-bounded-v3")

# Stop codes that mean "the model is repeating itself", which is the right way for a
# looping run to end.
LOOP_STOPS = {"CODING_NO_NEW_EVIDENCE"}


@pytest.fixture(params=PROFILES)
def profile(request: pytest.FixtureRequest) -> str:
    return request.param


def known_gap(
    request: pytest.FixtureRequest, profile: str, reason: str, profiles: tuple[str, ...] = PROFILES
) -> None:
    if profile in profiles:
        request.applymarker(pytest.mark.xfail(strict=True, reason=reason))


def run(  # noqa: PLR0913 - explicit trajectory knobs
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    turns: list[Turn],
    features: tuple[tuple[str, str], ...] = FEATURES,
    *,
    base_prompt_tokens: int = 4_000,
    prompt_growth_tokens: int = 600,
) -> Outcome:
    return drive(
        tmp_path,
        monkeypatch,
        Trajectory(
            turns,
            base_prompt_tokens=base_prompt_tokens,
            prompt_growth_tokens=prompt_growth_tokens,
        ),
        task=build_task(features),
        profile=profile,
    )


def feature_by_feature(features: tuple[tuple[str, str], ...] = FEATURES) -> list[Turn]:
    """Read once, then one exact edit and one probe run per feature (the repair rhythm)."""
    turns: list[Turn] = [call("read_file", path=GAME)]
    for name, _ in features:
        turns += [add_feature(name), call("check_acceptance_gameplay")]
    return turns


SMALL = FEATURES[:3]


def test_a_small_feature_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    outcome = run(tmp_path, monkeypatch, profile, feature_by_feature(SMALL), SMALL)
    assert outcome.state.status is RunStatus.SUCCEEDED, outcome.stop_summary


def test_steady_progress_through_one_probe_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str, request: pytest.FixtureRequest
) -> None:
    known_gap(
        request,
        profile,
        "F2: a probe failing at a later line scores the same, so steady progress "
        "through ten assertions stalls after eight verifications",
    )
    outcome = run(tmp_path, monkeypatch, profile, feature_by_feature())
    assert outcome.state.status is RunStatus.SUCCEEDED, outcome.stop_summary


def test_whole_file_rewrites_that_add_features_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str, request: pytest.FixtureRequest
) -> None:
    known_gap(request, profile, "F2: whole-file rewrites that add features look flat")
    turns: list[Turn] = []
    for count in range(1, len(FEATURES) + 1):
        content = "// Snake\n" + "".join(marker(name) for name, _ in FEATURES[:count])
        turns += [
            call("write_file", path=GAME, content=content),
            call("check_acceptance_gameplay"),
        ]
    outcome = run(tmp_path, monkeypatch, profile, turns)
    assert outcome.state.status is RunStatus.SUCCEEDED, outcome.stop_summary


def test_mistyped_edits_do_not_end_a_run_that_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    turns: list[Turn] = [call("read_file", path=GAME)]
    for _ in range(3):
        turns += [
            call("edit_file", path=GAME, old="text that is not there", new="x"),
            call("read_file", path=GAME),
        ]
    turns += feature_by_feature(SMALL)[1:]
    outcome = run(tmp_path, monkeypatch, profile, turns, SMALL)
    assert outcome.state.status is RunStatus.SUCCEEDED, outcome.stop_summary


def test_bad_read_arguments_do_not_end_a_run_that_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    turns: list[Turn] = [call("read_file", path=GAME, start_line="999")] * 3
    turns += feature_by_feature(SMALL)
    outcome = run(tmp_path, monkeypatch, profile, turns, SMALL)
    assert outcome.state.status is RunStatus.SUCCEEDED, outcome.stop_summary


def test_a_long_run_is_charged_for_new_work_not_the_repeated_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str, request: pytest.FixtureRequest
) -> None:
    known_gap(
        request,
        profile,
        "F3: every turn charges the whole prompt again, so a 1.5M budget ends a "
        "20K-prompt run near turn 48",
    )
    turns: list[Turn] = [call("read_file", path=GAME)]
    for name, _ in FEATURES:
        turns += [
            call("read_file", path=GAME),
            call("search_files", query=f"feature:{name}"),
            add_feature(name),
            call("check_acceptance_gameplay"),
            call("workspace_diff"),
            call("read_file", path="app/README.md"),
        ]
    outcome = run(
        tmp_path,
        monkeypatch,
        profile,
        turns,
        base_prompt_tokens=20_000,
        prompt_growth_tokens=500,
    )
    assert outcome.state.status is RunStatus.SUCCEEDED, outcome.stop_summary


def test_searches_with_new_results_are_not_repetition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str, request: pytest.FixtureRequest
) -> None:
    known_gap(
        request,
        profile,
        "no-new-evidence counts only read_file results, so searches that return new "
        "information still count as repetition",
    )
    turns: list[Turn] = [call("search_files", query=f"term{index}") for index in range(12)]
    turns += feature_by_feature(SMALL)
    outcome = run(tmp_path, monkeypatch, profile, turns, SMALL)
    assert outcome.state.status is RunStatus.SUCCEEDED, outcome.stop_summary


def test_a_reply_with_several_calls_does_not_end_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str, request: pytest.FixtureRequest
) -> None:
    known_gap(request, profile, "F9/R6: a reply with two tool calls is a model failure")
    both = call("read_file", path=GAME) + call("read_file", path="app/README.md")
    turns: list[Turn] = [both, both, both, *feature_by_feature(SMALL)]
    outcome = run(tmp_path, monkeypatch, profile, turns, SMALL)
    assert outcome.state.status is RunStatus.SUCCEEDED, outcome.stop_summary


def test_an_identical_failing_edit_forever_ends_as_a_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    turns: list[Turn] = [call("read_file", path=GAME)]
    turns += [call("edit_file", path=GAME, old="text that is not there", new="x")] * 40
    outcome = run(tmp_path, monkeypatch, profile, turns)
    assert outcome.state.status is not RunStatus.SUCCEEDED
    assert outcome.stop_code in LOOP_STOPS, outcome.stop_summary
    assert outcome.turns_consumed <= 20


def test_rerunning_a_check_on_an_unchanged_candidate_ends_as_a_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    turns: list[Turn] = [add_feature("start")]
    turns += [call("check_acceptance_gameplay")] * 40
    outcome = run(tmp_path, monkeypatch, profile, turns)
    assert outcome.state.status is not RunStatus.SUCCEEDED
    assert outcome.stop_code in LOOP_STOPS, outcome.stop_summary
    assert outcome.turns_consumed <= 20


def test_a_model_that_never_edits_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    outcome = run(tmp_path, monkeypatch, profile, [])
    assert outcome.state.status is not RunStatus.SUCCEEDED
    assert outcome.requests <= 20
