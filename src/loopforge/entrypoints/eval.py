"""Evaluation entrypoint wiring (PACS-016, M6): trial driver, report store, presets.

This module is the composition root for the M5 evaluation runner: it adapts
the locked workload bindings (which the layer-clean ``application`` runner
may never see) to the ``TrialDriver`` seam, persists operator-owned
``BenchmarkReport`` artifacts as JSON, and names the code-owned
configuration presets the CLI compares.

Authority boundaries:

- the driver only ever NARROWS runtime authority: the repair builders'
  default budgets are the code-owned envelope (``$1.00/8 iterations`` single
  runtime, ``$2.00/8`` orchestrated, stall threshold 3), and an
  ``EvalConfiguration`` tightens it per axis via ``min`` — budgets and the
  no-progress stall threshold alike, never widens (AGENTS.md rules 11, 12);
- sandbox mode is code-owned: ``CONTAINER`` tasks fail closed without a
  wired container image (rule 13), and ``TRUSTED_LOCAL`` tasks always run
  the trusted builder (their locked fixtures never run live) — with a
  fail-fast platform preflight so a host that rejects ``RLIMIT_AS`` names
  the true cause instead of spending the model's turns on sandbox-rejected
  commands first;
- reports are operator-owned artifacts: nothing in a run may write them,
  and reads fail closed — a corrupted, drifted, or tampered report file is
  rejected by revalidation through the domain constructors, never repaired
  or silently skipped.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from loopforge.adapters.fault_models import OutageModel, TransientFailureModel
from loopforge.adapters.memory import InMemoryEventStore
from loopforge.adapters.scripted import ScriptedModel
from loopforge.adapters.system_time import SystemClock, SystemSleeper
from loopforge.application.eval_runner import (
    EvalConfiguration,
    TrialRunResult,
)
from loopforge.application.graders import GraderEvidence
from loopforge.application.runtime import Runtime
from loopforge.domain.benchmarks import (
    BenchmarkCategory,
    BenchmarkReport,
    BenchmarkSandboxMode,
    BenchmarkTaskSpec,
    ConfigReport,
)
from loopforge.domain.context_lifecycle import ContextAccounting
from loopforge.domain.events import Event, VerificationFailed, VerificationPassed
from loopforge.domain.policies import ExecutionPolicy, resolve_policy
from loopforge.domain.routing import ModelTier
from loopforge.domain.security import SandboxCapabilities
from loopforge.domain.state import RunState
from loopforge.domain.types import ActionId, BudgetLimit, RunStatus
from loopforge.domain.verification import CheckOutcome
from loopforge.domain.workspace import FixtureFile
from loopforge.entrypoints.orchestrated import build_orchestrated_repair_runtime
from loopforge.entrypoints.repair import (
    RepairRuntimeDeps,
    build_container_repair_runtime,
    build_trusted_repair_runtime,
)
from loopforge.ports.clock import ClockPort, SleeperPort
from loopforge.ports.context import ContextAccountingSource
from loopforge.ports.model import ModelPort
from loopforge.ports.sandbox import SandboxCommandResult, SandboxPolicyError
from loopforge.ports.telemetry import TelemetryPort
from loopforge.ports.workspace import WorkspacePort
from loopforge.workloads.benchmarks import (
    AMBIGUOUS_NAIVE_SOLUTION,
    BenchmarkFaultKind,
    BenchmarkTaskBinding,
    build_benchmark_binding,
)
from loopforge.workloads.repair import (
    REPAIR_MODEL_REQUIREMENTS,
    OrchestratedRepairTask,
    RepairTask,
    scripted_repair_actions,
)

_TESTS_PREFIX: Final = "tests/"
_CONTAINER_EXECUTABLE: Final = "/usr/local/bin/python"

_TRUSTED_ENVELOPE: Final = BudgetLimit(max_cost_usd=1.0, max_iterations=8)
_ORCHESTRATED_ENVELOPE: Final = BudgetLimit(max_cost_usd=2.0, max_iterations=8)
# The builders' code-owned stall threshold (the runtime default): the eval
# configuration may tighten it, never widen it — same min-narrowing contract
# as the budget axes.
_ENVELOPE_NO_PROGRESS_LIMIT: Final = 3
# End-of-run evidence reads share the sandbox tool's byte cap
# (``SandboxLimits.max_read_bytes``): a trial-planted multi-GB fixture path
# must fail the trial loudly, never OOM the eval driver.
_EVIDENCE_READ_CAP_BYTES: Final = 1_000_000


class EvalTrialError(RuntimeError):
    """A trial's infrastructure failed; the eval fails loudly, never fabricates.

    A driver crash means the trial evidence is incomplete, so the honest
    outcome is to abort the eval with a code-owned error naming the trial —
    never to synthesize a minimal ``TrialRunResult`` whose graded "failure"
    would masquerade as a measured harness outcome.
    """


class EvalReportStoreError(RuntimeError):
    """The on-disk report store cannot be decoded or fails revalidation."""


class UnknownEvalReportError(EvalReportStoreError):
    """The addressed report id does not exist in the store.

    Distinct from store corruption (M7): an operator server maps this to 404
    while a corrupted, drifted, or tampered file stays a 500 — the client
    asked for something absent versus the server's store failing closed.
    Messages in this family never carry the absolute store path: the 404
    detail goes to the client, and the server's directory layout is not the
    client's business.
    """


class UnsafeEvalReportIdError(UnknownEvalReportError):
    """The addressed report id is not safe for the report store.

    A client-side addressing error (path traversal, separators, control
    characters), mapped to 404 like any other unknown report id — the house
    taxonomy treats an unaddressable resource as absent, never as the 500
    store-corruption family.
    """


_RLIMIT_PROBE: Final = (
    "import resource; resource.setrlimit(resource.RLIMIT_AS, (268435456, 268435456))"
)


def _rlimit_as_supported() -> bool:
    """Probe the platform prerequisite the trusted sandbox launcher needs.

    The launcher applies ``RLIMIT_AS`` in the child process; hosts that
    reject it (macOS: "resource limits rejected") make every fixture command
    fail closed. Same probe the test-suite sandbox gates use.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _RLIMIT_PROBE],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _container_for(image: str | None, spec: BenchmarkTaskSpec) -> str | None:
    """Resolve the sandbox path; CONTAINER tasks fail closed without an image."""
    if spec.sandbox_mode is BenchmarkSandboxMode.CONTAINER:
        if image is None:
            msg = (
                f"task {spec.task_id} declares container isolation (code-owned "
                "sandbox mode) but no container image was wired; pass --container IMAGE"
            )
            raise EvalTrialError(msg)
        return image
    # TRUSTED_LOCAL is code-owned and deliberate: the two fault-injection
    # fixtures are proven deterministic on the trusted builder and never run
    # live, so a wired image does not reroute them.
    return None


def _narrow_budget(config: EvalConfiguration, *, orchestrated: bool) -> BudgetLimit:
    """Tighten the builders' code-owned envelope per axis; never widen it."""
    envelope = _ORCHESTRATED_ENVELOPE if orchestrated else _TRUSTED_ENVELOPE
    return BudgetLimit(
        max_cost_usd=min(config.max_cost_usd, envelope.max_cost_usd),
        max_iterations=min(config.max_iterations, envelope.max_iterations),
    )


def _narrow_no_progress_limit(config: EvalConfiguration) -> int:
    """Tighten the stall threshold against the code-owned envelope; never widen it."""
    return min(config.no_progress_limit, _ENVELOPE_NO_PROGRESS_LIMIT)


def _policy_for(config: EvalConfiguration) -> ExecutionPolicy | None:
    """Resolve a configuration's candidate policy reference, fail closed.

    The registry is code-owned and versioned (PACS-017 M1): an unknown id
    or version raises ``UnknownPolicyError`` — a trial can never run under
    an invented policy.
    """
    if config.policy_id is None:
        return None
    return resolve_policy(config.policy_id, version=config.policy_version)


class _CheckNameProbeSandbox:
    """``SandboxPort`` stub that lets a code-owned check declare its outcome name.

    ``RepairVerifier`` embeds each hook's own ``CheckOutcome.name`` in its
    composed outcomes, so the grader's required names are whatever the
    code-owned checks declare. Asking the checks directly keeps that
    provenance in code: command runs return a canned non-succeeded result
    (nothing executes) and the file APIs fail closed, so only the outcome's
    NAME is consumed. The stub truthfully claims no enforced capabilities.
    """

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            file_api_confined=False,
            symlink_protected=False,
            environment_filtered=False,
            process_timeout=False,
            resource_limits=False,
            output_limited=False,
            process_filesystem_isolated=False,
            network_isolated=False,
            kernel_isolated=False,
        )

    def read_text(self, relative_path: str) -> str:
        msg = f"check-name probe stub does not read files: {relative_path!r}"
        raise SandboxPolicyError(msg)

    def write_text(self, relative_path: str, content: str) -> None:
        del content  # the probe writes nothing
        msg = f"check-name probe stub does not write files: {relative_path!r}"
        raise SandboxPolicyError(msg)

    def run(
        self, command_name: str, *, timeout_seconds: float | None = None
    ) -> SandboxCommandResult:
        del command_name, timeout_seconds  # the probe executes nothing
        return SandboxCommandResult(exit_code=1, stdout="", stderr="", succeeded=False)


_CHECK_NAME_PROBE_SANDBOX: Final = _CheckNameProbeSandbox()


def _check_names_from_checks(
    binding: BenchmarkTaskBinding, workspace: WorkspacePort
) -> tuple[str, ...]:
    """Hook check names, asked of the code-owned checks themselves.

    The names M3's ground-truth grader re-checks are the ones
    ``RepairVerifier`` embeds in outcomes — each hook's own
    ``CheckOutcome.name``. Deriving them from ``binding.checks`` (never by
    scraping the durable stream) keeps the grader input code-owned:
    verification detail strings embed unquoted, model-influenceable
    workspace filenames, so a file named ``x; evil: failed`` must not be
    able to inject ``evil`` into the required set.
    """
    names: list[str] = []
    for check in binding.checks:
        try:
            outcome = check(_CHECK_NAME_PROBE_SANDBOX, workspace)
        except Exception as exc:
            msg = (
                f"acceptance check {getattr(check, '__name__', repr(check))} could not "
                f"declare its outcome name: {type(exc).__name__}: {exc}"
            )
            raise EvalTrialError(msg) from exc
        # Boundary validation mirrors RepairVerifier's: hooks are code-owned,
        # but a contract violation fails the trial loudly, never silently.
        if not isinstance(outcome, CheckOutcome):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg_2 = (
                f"acceptance check {getattr(check, '__name__', repr(check))} returned "
                f"{type(outcome).__name__}, expected CheckOutcome"
            )
            raise EvalTrialError(msg_2)
        if outcome.name not in names:
            names.append(outcome.name)
    return tuple(sorted(names))


def _accountings_of(context_builder: object) -> tuple[ContextAccounting, ...]:
    """The context builder's accounting ledger, when the seam exposes one.

    ``ContextAccountingSource.last_accounting`` carries the most recent
    per-assembly ledger (the same ledger M4's precedence pin reads); the
    peak-over-accountings metric over a single real ledger stays honest.
    """
    if isinstance(context_builder, ContextAccountingSource):
        accounting = context_builder.last_accounting
        if accounting is not None:
            return (accounting,)
    return ()


@dataclass(frozen=True, slots=True)
class _TrialWiring:
    """Per-trial bundle-building wiring shared by the single/orchestrated paths."""

    deps: RepairRuntimeDeps
    workspaces_dir: Path
    container: str | None


class EvalTrialDriver:
    """Production ``TrialDriver``: builds, faults, drives, and evidences one trial.

    Wiring mirrors the CLI's repair builders: ``workspaces_root`` parents a
    FRESH workspace directory per trial (trial isolation — workspace dirs are
    never reused); when no root is wired the driver mkdtemps one itself and
    REMOVES it after the trial's evidence is collected (driver-created,
    driver-cleaned — the CLI's own TemporaryDirectory contract stays with the
    CLI); ``model_factory`` builds a live model per trial (``None``
    wires the deterministic scripted model, per-worker for the orchestrated
    binding); ``container_image`` selects the untrusted sandbox path for
    ``CONTAINER`` tasks. ``router_enabled`` on the configuration is
    descriptive today: the repair builders always wire the code-owned
    single-entry ``TieredRoutingPolicy``, so model selection is identical
    either way — the flag is carried for later exploration configurations
    with a multi-model registry, and toggling it changes nothing here.
    """

    def __init__(  # noqa: PLR0913 - composition wiring keeps every seam explicit
        self,
        *,
        workspaces_root: str | Path | None = None,
        model_factory: Callable[[RepairTask | OrchestratedRepairTask], ModelPort] | None = None,
        model_tier: ModelTier = ModelTier.ECONOMY,
        container_image: str | None = None,
        clock_factory: Callable[[], ClockPort] = SystemClock,
        sleeper_factory: Callable[[], SleeperPort] = SystemSleeper,
        telemetry_factory: Callable[[], TelemetryPort] | None = None,
        executable: str | None = None,
    ) -> None:
        self._workspaces_root = Path(workspaces_root) if workspaces_root is not None else None
        self._model_factory = model_factory
        self._model_tier = model_tier
        self._container_image = container_image
        self._clock_factory = clock_factory
        self._sleeper_factory = sleeper_factory
        self._telemetry_factory = telemetry_factory
        self._executable = executable
        self._trusted_platform_ok: bool | None = None
        # Directories the driver itself mkdtemp'd (no operator root wired):
        # the driver owns their lifecycle and removes them after each trial.
        self._fallback_workspaces_dirs: set[Path] = set()

    def __call__(
        self, spec: BenchmarkTaskSpec, config: EvalConfiguration, trial_id: str
    ) -> TrialRunResult:
        try:
            return self._drive_trial(spec, config, trial_id)
        except EvalTrialError:
            raise
        except Exception as exc:
            msg = f"trial {trial_id} failed with {type(exc).__name__}: {exc}"
            raise EvalTrialError(msg) from exc

    def _drive_trial(
        self, spec: BenchmarkTaskSpec, config: EvalConfiguration, trial_id: str
    ) -> TrialRunResult:
        container = _container_for(self._container_image, spec)
        self._require_trusted_platform(spec)
        executable = self._executable or (_CONTAINER_EXECUTABLE if container else None)
        binding = build_benchmark_binding(spec.task_id, executable=executable)
        task = binding.task
        store = InMemoryEventStore()
        policy = _policy_for(config)
        deps = RepairRuntimeDeps(
            store=store,
            clock=self._clock_factory(),
            sleeper=self._sleeper_factory(),
            telemetry=self._telemetry_factory() if self._telemetry_factory is not None else None,
            model=self._model_for(binding),
            model_tier=(policy.routing.default_tier if policy is not None else self._model_tier),
            budget=_narrow_budget(config, orchestrated=isinstance(task, OrchestratedRepairTask)),
            no_progress_limit=_narrow_no_progress_limit(config),
            verify_read_only_turns=(
                policy.verify_read_only_turns
                if policy is not None
                else config.verify_read_only_turns
            ),
            context_allocation=policy.context_allocation if policy is not None else None,
            routing=(
                policy.routing.for_requirements(REPAIR_MODEL_REQUIREMENTS)
                if policy is not None
                else None
            ),
        )
        wiring = _TrialWiring(
            deps=deps,
            workspaces_dir=self._trial_workspaces_dir(trial_id),
            container=container,
        )
        if isinstance(task, OrchestratedRepairTask):
            return self._drive_orchestrated(binding, wiring)
        return self._drive_single(binding, config, wiring)

    def _require_trusted_platform(self, spec: BenchmarkTaskSpec) -> None:
        """Fail fast when the host cannot run the trusted sandbox (rule 13).

        Without this preflight the trial still fails closed — the launcher
        rejects every fixture command — but the true cause (the platform
        rejects ``setrlimit(RLIMIT_AS)``) surfaces only as a confusing
        downstream error after the model's turns are spent. Name it
        immediately instead; the probe runs once per driver.
        """
        if spec.sandbox_mode is not BenchmarkSandboxMode.TRUSTED_LOCAL:
            return
        if self._trusted_platform_ok is None:
            self._trusted_platform_ok = _rlimit_as_supported()
        if not self._trusted_platform_ok:
            msg = (
                f"task {spec.task_id} requires the trusted local sandbox, but this "
                "platform rejects setrlimit(RLIMIT_AS); the launcher cannot apply "
                "resource limits, so fixture commands would fail closed — run the "
                "eval on a platform where the trusted sandbox is supported"
            )
            raise EvalTrialError(msg)

    def _model_for(self, binding: BenchmarkTaskBinding) -> ModelPort | None:
        task = binding.task
        model: ModelPort | None
        if self._model_factory is not None:
            model = self._model_factory(task)
        elif isinstance(task, RepairTask):
            model = ScriptedModel(scripted_repair_actions(task))
        else:
            # The orchestrated builder wires one scripted model per worker
            # assignment when deps.model is None; a single shared scripted
            # instance would misalign the per-worker action scripts.
            model = None
        if model is not None and binding.fault is not None:
            model = _wrap_fault(binding, model)
        return model

    def _trial_workspaces_dir(self, trial_id: str) -> Path:
        if self._workspaces_root is not None:
            return self._workspaces_root / trial_id.replace(":", "_")
        # Driver-owned fallback: the driver created this directory, so the
        # driver removes it once the trial's evidence is collected (the CLI's
        # own TemporaryDirectory contract stays with the CLI).
        directory = Path(tempfile.mkdtemp(prefix=f"loopforge-eval-{trial_id.replace(':', '_')}-"))
        self._fallback_workspaces_dirs.add(directory)
        return directory

    def _release_workspaces_dir(self, workspaces_dir: Path) -> None:
        """Remove a driver-created fallback dir; never an operator-supplied root.

        Best-effort (``ignore_errors``): a cleanup failure must never mask the
        trial's own outcome, and the residue is an operator-visible tmp dir,
        not lost evidence.
        """
        if workspaces_dir not in self._fallback_workspaces_dirs:
            return
        self._fallback_workspaces_dirs.discard(workspaces_dir)
        shutil.rmtree(workspaces_dir, ignore_errors=True)

    def _drive_single(
        self,
        binding: BenchmarkTaskBinding,
        config: EvalConfiguration,
        wiring: _TrialWiring,
    ) -> TrialRunResult:
        task = binding.task
        assert isinstance(task, RepairTask)  # narrow: orchestrated handled elsewhere
        approvals = frozenset(binding.approval_required_for)
        if wiring.container is not None:
            bundle = build_container_repair_runtime(
                task,
                image=wiring.container,
                workspaces_dir=wiring.workspaces_dir,
                deps=wiring.deps,
                checks=binding.checks,
                approval_required_for=approvals,
            )
        else:
            bundle = build_trusted_repair_runtime(
                task,
                workspaces_dir=wiring.workspaces_dir,
                deps=wiring.deps,
                checks=binding.checks,
                approval_required_for=approvals,
            )
        try:
            state = self._drive_with_approvals(bundle.runtime, task, config)
            events = tuple(wiring.deps.store.events_for(state.run_id))
            evidence = _evidence_for(binding, bundle.workspace, events)
            accountings = _accountings_of(bundle.runtime.context)
        finally:
            bundle.close()
            self._release_workspaces_dir(wiring.workspaces_dir)
        return TrialRunResult(
            run_id=str(state.run_id),
            status=state.status,
            events=events,
            evidence=evidence,
            context_accountings=accountings,
        )

    def _drive_orchestrated(
        self,
        binding: BenchmarkTaskBinding,
        wiring: _TrialWiring,
    ) -> TrialRunResult:
        task = binding.task
        assert isinstance(task, OrchestratedRepairTask)  # narrow: checked by caller
        bundle = build_orchestrated_repair_runtime(
            task,
            workspaces_dir=wiring.workspaces_dir,
            deps=wiring.deps,
            container_image=wiring.container,
        )
        try:
            state = bundle.orchestrator.run(task.objective, task.plan)
            events = tuple(wiring.deps.store.events_for(state.run_id))
            evidence = _evidence_for(binding, bundle.integration_workspace, events)
            # The bundle does not expose the per-worker context builders, so
            # no accounting ledger is collectible here: the M4 metrics
            # honestly degrade to billed usage for orchestrated trials.
            accountings: tuple[ContextAccounting, ...] = ()
        finally:
            bundle.close()
            self._release_workspaces_dir(wiring.workspaces_dir)
        return TrialRunResult(
            run_id=str(state.run_id),
            status=state.status,
            events=events,
            evidence=evidence,
            context_accountings=accountings,
        )

    @staticmethod
    def _drive_with_approvals(
        runtime: Runtime, task: RepairTask, config: EvalConfiguration
    ) -> RunState:
        """Drive to quiescence, programmatically granting each pending approval.

        The grant is the counted human intervention (``ApprovalGranted`` is
        durable; M4's ``count_human_interventions`` sees it). Termination is
        guaranteed by the iteration budget — every granted action executes
        exactly once and burns one iteration — and the defensive grant bound
        turns a wiring bug into an ``EvalTrialError`` instead of a hang.
        """
        state = runtime.run(task.objective)
        grants = 0
        while state.status is RunStatus.WAITING_FOR_APPROVAL:
            if grants >= config.max_iterations:
                msg = (
                    f"approval grant bound ({config.max_iterations}) exceeded; "
                    "the approval wiring cannot terminate this run"
                )
                raise EvalTrialError(msg)
            pending = state.current_action_id
            if pending is None:
                msg_2 = "run is waiting for approval but names no pending action"
                raise EvalTrialError(msg_2)
            state = runtime.grant_approval(state.run_id, ActionId(pending))
            grants += 1
            state = runtime.resume(state.run_id)
        return state


def _wrap_fault(binding: BenchmarkTaskBinding, model: ModelPort) -> ModelPort:
    """Map the binding's code-owned fault descriptor to an M5 decorator."""
    fault = binding.fault
    assert fault is not None  # narrow: checked by caller
    if fault.kind is BenchmarkFaultKind.PROVIDER_OUTAGE:
        return OutageModel(model)
    if fault.kind is BenchmarkFaultKind.TRANSIENT_API_FAILURE:
        return TransientFailureModel(model, fault.transient_failure_count)
    msg = f"unknown benchmark fault kind: {fault.kind!r}"
    raise EvalTrialError(msg)


def _read_evidence_text(workspace_root: Path, relative_path: str) -> str:
    """Bounded, symlink-safe read of one end-of-run evidence file.

    A live-model trial controls the workspace at evidence time, so a bare
    ``Path.read_text`` on a fixture path is an attack surface: a planted
    symlink (``/dev/zero`` hangs the driver), a multi-GB file (OOM), or a
    binary file (``UnicodeDecodeError`` killing the WHOLE eval). Mirrors the
    sandbox's own defenses (``local_sandbox`` byte cap + symlink rejection):
    any symlink component under the workspace root, any escape of the root,
    a non-regular or oversize (``_EVIDENCE_READ_CAP_BYTES``) target, and any
    undecodable/unreadable content all raise ``EvalTrialError`` — a trial
    whose evidence cannot be collected fails loudly by name, never silently
    fabricates, and never aborts the eval with a raw exception.
    """
    root = workspace_root.resolve()
    current = root
    for part in relative_path.split("/"):
        current = current / part
        if current.is_symlink():
            msg = (
                f"evidence path {relative_path!r} contains a symlink component; "
                "a trial-planted link cannot name grader evidence"
            )
            raise EvalTrialError(msg)
    try:
        resolved = current.resolve(strict=True)
    except FileNotFoundError as exc:
        msg = f"evidence path {relative_path!r} vanished during collection"
        raise EvalTrialError(msg) from exc
    except OSError as exc:
        msg_2 = f"evidence path {relative_path!r} cannot be resolved: {exc}"
        raise EvalTrialError(msg_2) from exc
    if not resolved.is_relative_to(root):
        msg_3 = f"evidence path {relative_path!r} escapes the workspace root"
        raise EvalTrialError(msg_3)
    try:
        if not resolved.is_file():
            msg_4 = f"evidence path {relative_path!r} is not a regular file"
            raise EvalTrialError(msg_4)
        if resolved.stat().st_size > _EVIDENCE_READ_CAP_BYTES:
            msg_5 = (
                f"evidence path {relative_path!r} exceeds the "
                f"{_EVIDENCE_READ_CAP_BYTES}-byte evidence read cap"
            )
            raise EvalTrialError(msg_5)
        # newline="" mirrors the sandbox read: evidence bytes stay faithful
        # (CRLF included), never silently rewritten.
        return resolved.read_text(encoding="utf-8", newline="")
    except UnicodeDecodeError as exc:
        msg_6 = f"evidence path {relative_path!r} is not valid utf-8 text"
        raise EvalTrialError(msg_6) from exc
    except PermissionError as exc:
        msg_7 = f"evidence path {relative_path!r} is not readable"
        raise EvalTrialError(msg_7) from exc
    except OSError as exc:
        msg_8 = f"evidence path {relative_path!r} read failed: {exc}"
        raise EvalTrialError(msg_8) from exc


def _evidence_for(
    binding: BenchmarkTaskBinding,
    workspace: WorkspacePort,
    events: tuple[Event, ...],
) -> GraderEvidence:
    """Project the end-of-run workspace and durable stream into grader evidence.

    Test-file truth is three-way explicit: surviving files with on-disk
    content (read bounded and symlink-safe via ``_read_evidence_text``),
    ``deleted_test_files`` for fixture test files that no longer exist (a
    deletion is evidence, never a silent absence), and the locked fixture
    originals as the expected ground truth. ``required_check_names`` derives
    from the code-owned ``binding.checks`` (never scraped from the durable
    stream — see ``_check_names_from_checks``);
    ``passing_verification_summaries`` carries only the ``VerificationPassed``
    summaries (the anti-forgery split the hook-evidence re-check trusts);
    ``naive_solution`` binds the known-wrong patch for the ambiguous-success
    category.
    """
    fixture = binding.task.fixture
    workspace_root = workspace.root
    test_files: list[FixtureFile] = []
    deleted: list[str] = []
    for item in fixture.files:
        if not item.path.startswith(_TESTS_PREFIX):
            continue
        on_disk = workspace_root / item.path
        if not on_disk.exists():
            deleted.append(item.path)
            continue
        content = _read_evidence_text(workspace_root, item.path)
        test_files.append(FixtureFile(path=item.path, content=content))
    sources = tuple(
        FixtureFile(path=item.path, content=_read_evidence_text(workspace_root, item.path))
        for item in fixture.files
        if not item.path.startswith(_TESTS_PREFIX) and (workspace_root / item.path).exists()
    )
    summaries = tuple(
        event.summary
        for event in events
        if isinstance(event, VerificationPassed | VerificationFailed)
    )
    passing = tuple(event.summary for event in events if isinstance(event, VerificationPassed))
    naive: tuple[FixtureFile, ...] = ()
    if binding.spec.category is BenchmarkCategory.AMBIGUOUS_SUCCESS:
        naive = AMBIGUOUS_NAIVE_SOLUTION
    return GraderEvidence(
        final_changed_files=tuple(sorted(workspace.status().files)),
        test_files=tuple(test_files),
        deleted_test_files=tuple(deleted),
        expected_test_files=tuple(
            item for item in fixture.files if item.path.startswith(_TESTS_PREFIX)
        ),
        final_sources=sources,
        verification_summaries=summaries,
        passing_verification_summaries=passing,
        required_check_names=_check_names_from_checks(binding, workspace),
        naive_solution=naive,
    )


# ---------------------------------------------------------------------------
# Code-owned configuration presets.
# ---------------------------------------------------------------------------

_EVAL_PRESETS: Final = {
    # House defaults, identical to the trusted builder's envelope.
    "baseline": EvalConfiguration(
        config_id="baseline",
        max_cost_usd=1.0,
        max_iterations=8,
    ),
    # Contrasting budget-narrowed runtime configuration: half the iteration
    # envelope and a 5-cent cost ceiling, with a tighter stall threshold.
    "tight-budget": EvalConfiguration(
        config_id="tight-budget",
        max_cost_usd=0.05,
        max_iterations=4,
        no_progress_limit=2,
    ),
    # PACS-016 M8 A/B arm: the legacy every-turn verification cadence
    # (read-only turns verify and accrue no-progress strikes), so the
    # laboratory compares runtime configurations — tuned default vs legacy
    # exploration-vs-no-progress behavior — not model brands.
    "legacy-progress": EvalConfiguration(
        config_id="legacy-progress",
        max_cost_usd=1.0,
        max_iterations=8,
        verify_read_only_turns=True,
    ),
    # PACS-017 M5 candidate-policy arms: each carries a versioned built-in
    # ExecutionPolicy reference (resolved fail-closed against the code-owned
    # registry at trial time), so candidate-vs-active comparisons run
    # through the same locked laboratory as every other configuration.
    "policy-baseline": EvalConfiguration(
        config_id="policy-baseline",
        max_cost_usd=1.0,
        max_iterations=8,
        policy_id="baseline",
        policy_version=1,
    ),
    "policy-adaptive-context": EvalConfiguration(
        config_id="policy-adaptive-context",
        max_cost_usd=1.0,
        max_iterations=8,
        policy_id="adaptive-context",
        policy_version=1,
    ),
    "policy-patient-router": EvalConfiguration(
        config_id="policy-patient-router",
        max_cost_usd=1.0,
        max_iterations=8,
        policy_id="patient-router",
        policy_version=1,
    ),
}


def resolve_configurations(names: tuple[str, ...]) -> tuple[EvalConfiguration, ...]:
    """Resolve preset names to configurations; unknown or duplicated names fail closed."""
    if not names:
        msg = "at least one eval configuration preset is required"
        raise ValueError(msg)
    configs: list[EvalConfiguration] = []
    for name in names:
        config = _EVAL_PRESETS.get(name)
        if config is None:
            known = ", ".join(sorted(_EVAL_PRESETS))
            msg_2 = f"unknown eval configuration preset {name!r}; known presets: {known}"
            raise ValueError(msg_2)
        configs.append(config)
    config_ids = [config.config_id for config in configs]
    if len(set(config_ids)) != len(config_ids):
        msg_3 = "eval configuration presets must be unique"
        raise ValueError(msg_3)
    return tuple(configs)


# ---------------------------------------------------------------------------
# Operator-owned JSON report store.
# ---------------------------------------------------------------------------

REPORT_SCHEMA_VERSION: Final = 2
"""Current report schema version. v2 (PACS-017 M5) adds the context-efficiency
and recovery means to ``ConfigReport``; v1 artifacts stay loadable through the
migration shim in ``_report_from_dict`` (missing means fill the honest zero
default — v1 files never recorded them)."""

_REPORT_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_REPORT_KEYS: Final = frozenset(
    {"report_id", "suite_version", "lock_hash", "config_reports", "pareto_config_ids"}
)
_CONFIG_REPORT_KEYS_V1: Final = frozenset(
    {
        "config_id",
        "task_id",
        "trials",
        "successes",
        "false_successes",
        "success_rate",
        "false_success_rate",
        "mean_cost_usd",
        "mean_latency_seconds",
        "mean_total_tokens",
        "mean_human_interventions",
    }
)
_CONFIG_REPORT_KEYS: Final = _CONFIG_REPORT_KEYS_V1 | frozenset(
    {
        "mean_context_tokens_used",
        "mean_context_items_dropped",
        "mean_recovery_events",
    }
)
_CONFIG_REPORT_V2_DEFAULTS: Final = {
    "mean_context_tokens_used": 0.0,
    "mean_context_items_dropped": 0.0,
    "mean_recovery_events": 0.0,
}


@dataclass(frozen=True, slots=True, kw_only=True)
class EvalReportSummary:
    """Honest list-view of one stored report, derived from its content only."""

    report_id: str
    suite_version: str
    lock_hash: str
    config_ids: tuple[str, ...]
    task_ids: tuple[str, ...]
    created_at: str


def report_to_dict(report: BenchmarkReport) -> dict[str, object]:
    """JSON-safe serialization of one report; the exact shape the store saves.

    Shared by the atomic writer and the M7 operator server so the wire
    projection of a report is byte-identical to its operator-owned artifact.
    """
    return {
        "report_id": report.report_id,
        "suite_version": report.suite_version,
        "lock_hash": report.lock_hash,
        "config_reports": [
            {
                "config_id": entry.config_id,
                "task_id": entry.task_id,
                "trials": entry.trials,
                "successes": entry.successes,
                "false_successes": entry.false_successes,
                "success_rate": entry.success_rate,
                "false_success_rate": entry.false_success_rate,
                "mean_cost_usd": entry.mean_cost_usd,
                "mean_latency_seconds": entry.mean_latency_seconds,
                "mean_total_tokens": entry.mean_total_tokens,
                "mean_human_interventions": entry.mean_human_interventions,
                "mean_context_tokens_used": entry.mean_context_tokens_used,
                "mean_context_items_dropped": entry.mean_context_items_dropped,
                "mean_recovery_events": entry.mean_recovery_events,
            }
            for entry in report.config_reports
        ],
        "pareto_config_ids": list(report.pareto_config_ids),
    }


def _require_keys(data: object, expected: frozenset[str], *, what: str) -> dict[str, object]:
    if not isinstance(data, dict):
        msg = f"{what} must be a JSON object"
        raise EvalReportStoreError(msg)
    # Keys are compared exactly against the code-owned expectation below, so
    # the decoded JSON object is safe to treat as str-keyed here.
    mapping = cast("dict[str, object]", data)
    keys = frozenset(mapping)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        msg_2 = f"{what} keys drifted (missing: {missing}, extra: {extra})"
        raise EvalReportStoreError(msg_2)
    return mapping


def _report_from_dict(data: object, *, schema_version: int) -> BenchmarkReport:
    """Rebuild a report through the domain constructors; tampering fails loudly.

    v1 artifacts predate the context/recovery means: their exact v1 key set
    is enforced first, then the missing means fill the honest zero default
    so domain revalidation stays the single authority for both versions.
    """
    fields = _require_keys(data, _REPORT_KEYS, what="eval report")
    entries = fields["config_reports"]
    if not isinstance(entries, list):
        msg = "eval report config_reports must be a list"
        raise EvalReportStoreError(msg)
    expected_keys = _CONFIG_REPORT_KEYS if schema_version == 2 else _CONFIG_REPORT_KEYS_V1
    try:
        reports: list[ConfigReport] = []
        for entry in cast("list[object]", entries):
            entry_fields = _require_keys(entry, expected_keys, what="config report")
            if schema_version == 1:
                entry_fields = {**_CONFIG_REPORT_V2_DEFAULTS, **entry_fields}
            reports.append(ConfigReport(**entry_fields))  # type: ignore[arg-type]
        config_reports = tuple(reports)
        return BenchmarkReport(
            report_id=fields["report_id"],  # type: ignore[arg-type]
            suite_version=fields["suite_version"],  # type: ignore[arg-type]
            lock_hash=fields["lock_hash"],  # type: ignore[arg-type]
            config_reports=config_reports,
            pareto_config_ids=tuple(fields["pareto_config_ids"]),  # type: ignore[arg-type]
        )
    except (TypeError, ValueError) as exc:
        msg_2 = f"eval report fails domain revalidation: {exc}"
        raise EvalReportStoreError(msg_2) from exc


class EvalReportStore:
    """JSON persistence for ``BenchmarkReport`` under an operator-owned directory.

    One file per report (``{report_id}.json``), atomic writes (tmp file +
    rename, stale-tmp swept on the writer side — a read-only construction
    never deletes, so opening a store cannot race a concurrent ``save``),
    fail-closed reads: a corrupted, wrong-version, drifted, or tampered file
    raises ``EvalReportStoreError`` — the domain constructors are the
    validation authority, so a hand-edited rate that disagrees with its
    counts cannot load.

    Honest boundary (M9 review disposition): revalidation proves
    CONSISTENCY (shape, keys, domain invariants), not provenance — the
    store is a trusted-local operator-owned artifact directory (D10);
    with write access to it an attacker can forge worse than a report.
    """

    def __init__(self, directory: str | Path, *, clock: ClockPort | None = None) -> None:
        self._dir = Path(directory)
        self._clock = clock or SystemClock()

    def _path_for(self, report_id: str) -> Path:
        if not _REPORT_ID_PATTERN.fullmatch(report_id):
            msg = (
                f"report_id {report_id!r} is not safe for the report store "
                "(letters, digits, dot, underscore, dash)"
            )
            raise UnsafeEvalReportIdError(msg)
        return self._dir / f"{report_id}.json"

    def _sweep_stale_tmp_files(self) -> None:
        """Remove interrupted-save residue, tolerating a concurrent save.

        Writer-side only (called from ``save``): a vanished tmp is another
        writer's completed rename, never an error.
        """
        for stale in self._dir.glob("*.tmp"):
            with suppress(FileNotFoundError):
                stale.unlink()

    def save(self, report: BenchmarkReport) -> Path:
        path = self._path_for(report.report_id)
        payload = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "created_at": self._clock.now().isoformat(),
            "report": report_to_dict(report),
        }
        self._dir.mkdir(parents=True, exist_ok=True)
        self._sweep_stale_tmp_files()
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            tmp_path.replace(path)
        except OSError as exc:
            # Losing a tmp-write/replace race (or any filesystem failure) at
            # the end of an expensive eval is a code-owned store error, never
            # a raw FileNotFoundError traceback.
            msg = f"eval report save failed for {report.report_id!r}: {exc}"
            raise EvalReportStoreError(msg) from exc
        return path

    def _decode(self, path: Path) -> tuple[str, BenchmarkReport]:
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # Never leak the absolute store path (the server's directory
            # layout is not the client's business) — file name only.
            msg = f"eval report file is not valid JSON: {path.name} ({exc})"
            raise EvalReportStoreError(msg) from exc
        except OSError as exc:
            # Unreadable artifact (permissions, glob/read race): server-side
            # corruption in the 500 family, never a bare plain-text 500.
            # ``str(exc)`` would embed the absolute path — strerror only.
            msg_0 = f"eval report file is unreadable: {path.name} ({exc.strerror})"
            raise EvalReportStoreError(msg_0) from exc
        envelope = _require_keys(
            raw,
            frozenset({"schema_version", "created_at", "report"}),
            what="eval report file",
        )
        version_marker = envelope["schema_version"]
        if (
            not isinstance(version_marker, int)
            or isinstance(version_marker, bool)
            or version_marker not in (1, REPORT_SCHEMA_VERSION)
        ):
            msg_2 = (
                f"eval report schema version {version_marker!r} is not "
                f"supported (expected 1..{REPORT_SCHEMA_VERSION}): {path.name}"
            )
            raise EvalReportStoreError(msg_2)
        created_at = envelope["created_at"]
        if not isinstance(created_at, str):
            msg_3 = f"eval report created_at must be a string: {path.name}"
            raise EvalReportStoreError(msg_3)
        return created_at, _report_from_dict(envelope["report"], schema_version=version_marker)

    def load(self, report_id: str) -> BenchmarkReport:
        path = self._path_for(report_id)
        if not path.is_file():
            msg = f"unknown eval report {report_id!r}"
            raise UnknownEvalReportError(msg)
        _created_at, report = self._decode(path)
        if report.report_id != report_id:
            msg_2 = (
                f"eval report file {path.name} carries report_id "
                f"{report.report_id!r}, expected {report_id!r}"
            )
            raise EvalReportStoreError(msg_2)
        return report

    def list(self) -> tuple[EvalReportSummary, ...]:
        if not self._dir.is_dir():
            return ()
        summaries: list[EvalReportSummary] = []
        for path in sorted(self._dir.glob("*.json")):
            created_at, report = self._decode(path)
            summaries.append(
                EvalReportSummary(
                    report_id=report.report_id,
                    suite_version=report.suite_version,
                    lock_hash=report.lock_hash,
                    config_ids=tuple(sorted({entry.config_id for entry in report.config_reports})),
                    task_ids=tuple(sorted({entry.task_id for entry in report.config_reports})),
                    created_at=created_at,
                )
            )
        return tuple(sorted(summaries, key=lambda summary: (summary.created_at, summary.report_id)))
