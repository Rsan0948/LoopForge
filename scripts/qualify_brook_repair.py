"""Isolated seeded Brook regression; never changes live task counters or branches.

Use the accepted calendar check/image and unchanged tests against a deliberately
broken copy. This is a qualification experiment, not a new attempt to fix an
exhausted live campaign task. Baseline must fail before any model call.
"""

import argparse
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import httpx

import loopforge.entrypoints.campaign_coding as coding_module
from loopforge.adapters.container_sandbox import ContainerSandbox, ContainerSandboxConfig
from loopforge.adapters.json_events import JsonEventCodec
from loopforge.adapters.local_sandbox import CommandSpec
from loopforge.adapters.model_lease import ModelLease
from loopforge.adapters.sqlite_events import SQLiteEventStore
from loopforge.entrypoints.campaign_coding import (
    build_coding,
    candidate_files,
    local_git,
    patch_identity,
    patch_review_objective,
    prepare_checkout,
)
from loopforge.entrypoints.campaign_evidence import review_obligations
from loopforge.entrypoints.campaign_manifest import release_manifest
from loopforge.entrypoints.campaign_models import CampaignConfig, CodingTaskConfig
from loopforge.entrypoints.campaign_review import build_review

SOURCE = "apps/dashboard/lib/workspace/export.ts"
ACCEPTED = "9eba77fa4680348b4d767da73ddee1c82bba0cfc"


def check_fixture(root: Path, task: CodingTaskConfig):
    check = task.checks[0]
    sandbox = ContainerSandbox(
        root,
        config=ContainerSandboxConfig(
            image=task.image,
            commands=(
                CommandSpec(
                    name=check.name, argv=tuple(check.argv), timeout_seconds=check.timeout_seconds
                ),
            ),
            workspace_read_only=True,
            environment={"PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp", "TMPDIR": "/tmp"},
        ),
    )
    return sandbox.run(check.name)


def main():  # noqa: PLR0912, PLR0915 - serial isolated qualification stages
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--frozen-seed", type=Path)
    parser.add_argument(
        "--profile", choices=("baseline-v1", "qwen-coding-v1"), default="baseline-v1"
    )
    args = parser.parse_args()
    args.output = args.output.resolve()
    with httpx.Client(timeout=10) as client:
        live = client.get("http://localhost:8124/campaigns").raise_for_status().json()["campaigns"]
        if any(c["state"]["status"] != "parked" or c["slot"] or c["worker_alive"] for c in live):
            msg = "Live campaigns must remain parked"
            raise RuntimeError(msg)
    args.output.mkdir(parents=True, exist_ok=False)
    ledger = Path(".loopforge/campaign").resolve()
    with sqlite3.connect(ledger / "campaigns.sqlite") as db:
        record = db.execute(
            "SELECT config FROM coding_tasks WHERE id=?", ("coding_02f99bb76e82",)
        ).fetchone()
    original = CodingTaskConfig.model_validate_json(record[0])
    source = ledger / "coding/coding_02f99bb76e82"
    if local_git(source, "rev-parse", "HEAD") != ACCEPTED:
        msg = "Accepted source identity changed"
        raise RuntimeError(msg)
    fixture = prepare_checkout(source, ACCEPTED, args.output / "seed", "qualification/seed")
    file = fixture.root / SOURCE
    good = file.read_text()
    if good.count("strIndex += cp.length;") != 1:
        msg = "Frozen seed target changed"
        raise RuntimeError(msg)
    control = check_fixture(fixture.root, original)
    if not control.succeeded:
        msg = "Accepted-source executable control failed; classify environment before coding"
        raise RuntimeError(msg)
    file.write_text(good.replace("strIndex += cp.length;", "strIndex += 1;"))
    seeded = check_fixture(fixture.root, original)
    if seeded.succeeded or seeded.exit_code != 1:
        msg = "Seed must cause an assertion failure, not an infrastructure failure"
        raise RuntimeError(msg)
    local_git(fixture.root, "add", "--", SOURCE)
    local_git(
        fixture.root,
        "-c",
        "user.name=LoopForge qualification",
        "-c",
        "user.email=qualification@localhost",
        "commit",
        "-m",
        "Frozen UTF16 regression seed",
    )
    revision = local_git(fixture.root, "rev-parse", "HEAD")
    if args.frozen_seed:
        frozen = args.frozen_seed.resolve()
        revision = local_git(frozen, "rev-parse", "HEAD")
        fixture = prepare_checkout(
            frozen, revision, args.output / "frozen-seed", "qualification/seed"
        )
        file = fixture.root / SOURCE
        if file.read_text() != good.replace("strIndex += cp.length;", "strIndex += 1;"):
            msg = "Frozen seed source differs from characterized regression"
            raise RuntimeError(msg)
        if set(fixture.run_git("diff", "--name-only", ACCEPTED).splitlines()) != {SOURCE}:
            msg = "Frozen seed changes files outside the single characterized regression"
            raise RuntimeError(msg)
    candidate = prepare_checkout(
        fixture.root, revision, args.output / "candidate", "qualification/candidate"
    )
    task = original.model_copy(
        update={
            "objective": "Correct calendar folding UTF8 round-trip corruption "
            "for mixed Unicode input. "
            "Use review_feedback for the immutable check, inspect the relevant source, "
            "make the smallest source-only correction. Existing tests remain immutable.",
            "branch": "qualification/candidate",
            "allowed_paths": [SOURCE],
            "evidence": "Frozen isolated seed derived from accepted Brook source; actual existing "
            "calendar regression must fail on seed and pass after correction.",
        }
    )
    config = CampaignConfig(
        repository=str(fixture.root),
        project="brook",
        revision=revision,
        scope=[SOURCE],
        inference_profile=args.profile,
        max_iterations=32,
        max_seconds=900,
        max_tokens=300000,
    )
    store = SQLiteEventStore(args.output / "runs.sqlite", codec=JsonEventCodec())
    # Capture every pre-request payload, including locally refused envelopes.
    original_model = coding_module.OllamaModel
    requests = []

    def occupancy(payload):
        requests.append(payload.copy())
        (args.output / "coding-requests.json").write_text(json.dumps(requests, indent=2))
        return len(json.dumps(payload, ensure_ascii=False).encode())

    def measured_model(**kwargs):
        return original_model(**kwargs, request_token_counter=occupancy)

    coding_module.OllamaModel = measured_model
    try:
        runtime, live_model = build_coding(config, task, candidate, store=store)
    finally:
        coding_module.OllamaModel = original_model
    baseline_id = runtime.start("Baseline characterization only")
    initial = runtime.verifier.verify(runtime.state_for(baseline_id))
    runtime.cancel(baseline_id, summary="Baseline characterization completed without inference")
    if initial.passed or initial.inconclusive:
        msg = "Seed must fail executable product checks before inference"
        raise RuntimeError(msg)
    (args.output / "identity.json").write_text(
        json.dumps(
            {
                "accepted": ACCEPTED,
                "loaded_manifest": release_manifest(),
                "seed": revision,
                "task": task.model_dump(),
                "config": config.model_dump(),
                "seed_source_sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                "baseline_check": initial.summary,
                "control_output": control.stdout,
                "seed_output": seeded.stdout + seeded.stderr,
            },
            indent=2,
        )
    )
    lease = ModelLease()
    lease.acquire("brook-qualification:" + str(args.output))
    started = time.monotonic()
    try:
        result = runtime.run(task.objective)
        identity = patch_identity(candidate)
        if live_model:
            live_model.close()
        files = candidate_files(candidate)
        expected = set(candidate.status().files) | {"CAMPAIGN_PATCH.diff"}
        review, tools, reviewer = build_review(
            config,
            files=files,
            previous="",
            store=store,
            patch_review=True,
            max_source_reads=task.max_review_reads,
        )
        tools.required_intervals = review_obligations(files, expected)
        tools.finalize_when_covered = True
        try:
            reviewed = review.run(patch_review_objective(task, expected))
        finally:
            if reviewer:
                reviewer.close()
        # New verifier instance: independent checks cannot inherit coder receipts.
        independent, checker = build_coding(config, task, candidate, store=store)
        try:
            verified = independent.verifier.verify(result)
        finally:
            if checker:
                checker.close()
        report = tools.report
        passed = (
            result.status.value == "succeeded"
            and verified.passed
            and reviewed.status.value == "succeeded"
            and report is not None
            and not report.unresolved
            and all(f.disposition == "rejected" for f in report.findings)
            and patch_identity(candidate) == identity
            and candidate.status().files == (SOURCE,)
        )
        receipt = {
            "passed": passed,
            "profile": args.profile,
            "coding_status": result.status.value,
            "review_status": reviewed.status.value,
            "patch_sha256": identity,
            "checks": verified.summary,
            "report": report.model_dump() if report else None,
            "seconds": time.monotonic() - started,
            "tokens": result.total_tokens,
            "provider_observations": live_model.request_tracker.observations()
            if live_model
            else [],
            "limitation": (
                "Single seeded regression from real Brook source; "
                "not general repair quality or a live fix"
            ),
        }
        (args.output / "result.json").write_text(json.dumps(receipt, indent=2))
        (args.output / "candidate.diff").write_text(candidate.diff())
        print(
            json.dumps(
                {
                    k: receipt[k]
                    for k in (
                        "passed",
                        "profile",
                        "coding_status",
                        "review_status",
                        "seconds",
                        "tokens",
                    )
                }
            ),
            flush=True,
        )
    finally:
        if live_model:
            live_model.close()
        lease.release()


if __name__ == "__main__":
    main()
