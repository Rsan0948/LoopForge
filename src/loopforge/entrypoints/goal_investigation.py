"""Scoped pinned-source inspection, approved environment preparation and reproduction."""

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from loopforge.adapters.container_sandbox import ContainerSandbox, ContainerSandboxConfig
from loopforge.adapters.local_sandbox import CommandSpec
from loopforge.adapters.research_store import ResearchStore
from loopforge.domain.evidence_pages import evidence_page
from loopforge.domain.goals import GoalAction, GoalState, SupervisorDecision
from loopforge.domain.investigation import FailureKind, ResearchRecord, TaskAssignment
from loopforge.entrypoints.campaign_coding import (
    CODING_REQUIREMENTS,
    coding_sandbox_limits,
    local_git_bytes,
    prepare_checkout,
)
from loopforge.entrypoints.campaign_evidence import source_allowed
from loopforge.entrypoints.campaign_models import CodingTaskConfig
from loopforge.entrypoints.goal_checkpoint import capture, snapshot_checkpoint
from loopforge.entrypoints.repository_goal_models import ApprovedEnvironment, RepositoryGoalConfig
from loopforge.ports.sandbox import SandboxError
from loopforge.ports.supervisor import InvestigationFailure
from loopforge.workloads.repair import acceptance_failure_line

MAX_ARTIFACT_BYTES = 262_144


def within(path: str, scopes: list[str]) -> bool:
    return any(path == scope or path.startswith(scope.rstrip("/") + "/") for scope in scopes)


class RepositoryInvestigation:
    def __init__(
        self,
        config: RepositoryGoalConfig,
        directory: Path,
        store: ResearchStore,
        candidate: Callable[[], tuple[Path, str]] | None = None,
    ) -> None:
        self._config_json = config.model_dump_json()
        self.directory, self.store = directory, store
        self.candidate = candidate

    @property
    def config(self) -> RepositoryGoalConfig:
        return RepositoryGoalConfig.model_validate_json(self._config_json)

    def environment(self, identity: str) -> ApprovedEnvironment:
        for environment in self.config.environments:
            if environment.environment_id == identity:
                return environment
        msg = "Environment is outside the approved catalog"
        raise ValueError(msg)

    def task(self, state: GoalState) -> CodingTaskConfig:
        assignment = state.assignment
        if assignment is None:
            msg = "Worker task has not been prepared"
            raise ValueError(msg)
        return self._task(assignment, state, validate=False)

    def _task(
        self, assignment: TaskAssignment | None, state: GoalState, *, validate: bool = True
    ) -> CodingTaskConfig:
        if validate and state.observation.status in {"coding", "reviewing", "queued"}:
            msg = "Assignment revision requires reconciled idle work"
            raise ValueError(msg)
        if assignment is None:
            msg = "Delegate requires a strict task assignment"
            raise ValueError(msg)
        config = self.config
        if (validate and (not state.environment_ready or not state.baseline_reproduced)) or len(
            assignment.files
        ) > config.task.max_changed_files:
            msg = "Assignment requires prepared environment, reproduced behavior and bounded scope"
            raise ValueError(msg)
        if any(
            not source_allowed(path) or not within(path, config.task.allowed_paths)
            for path in assignment.files
        ):
            msg = "Assignment expands authorized edit scope"
            raise ValueError(msg)
        records = {record.record_id: record for record in state.research}
        if any(identity not in records for identity in assignment.evidence_ids) or not any(
            records[identity].kind == "source" for identity in assignment.evidence_ids
        ):
            msg = "Assignment must depend on observed original source evidence"
            raise ValueError(msg)
        environment = self.environment(
            state.environment_id
            if validate or not state.assignment_versions
            else state.assignment_versions[-1].environment_id
        )
        # Model objective remains lower-trust assignment guidance. Original goal,
        # criteria, limits, branch, image, commands and acceptance are code-resolved.
        values = config.task.model_dump()
        allowed = sorted(
            set(assignment.files)
            | {path for version in state.assignment_versions for path in version.assignment.files}
        )
        values["editable_project_paths"] = [
            path for path in config.task.editable_project_paths if within(path, allowed)
        ]
        values.update(
            {
                "objective": config.task.objective,
                "branch": (
                    (state.assignment_versions[-1].branch or config.task.branch)
                    if not validate and state.assignment_versions
                    else state.experiments[-1].branch
                    if state.experiments
                    else config.task.branch
                )
                + (
                    f"/milestone-{len(state.assignment_versions)}"
                    if len(state.assignment_versions) > 1
                    else ""
                ),
                "allowed_paths": sorted(
                    set(assignment.files)
                    | {
                        path
                        for version in state.assignment_versions
                        for path in version.assignment.files
                    }
                ),
                "required_changed_paths": list(config.task.required_changed_paths),
                "image": environment.image,
                "checks": [check.model_dump() for check in environment.checks],
                "evidence": "Prepared from pinned original evidence: "
                + ", ".join(assignment.evidence_ids),
            }
        )
        return CodingTaskConfig.model_validate(values)

    def perform(self, decision: SupervisorDecision, key: str, state: GoalState) -> ResearchRecord:  # noqa: PLR0912, PLR0915 - explicit boundary dispatch/normalization
        intent = hashlib.sha256(json.dumps(asdict(decision), sort_keys=True).encode()).hexdigest()
        prior = self.store.prior(key, intent)
        if prior is not None:
            return prior
        if decision.action == GoalAction.INSPECT:
            raw, reference, kind, excerpt, truncated = self._inspect(decision)
            request = decision.inspection
            assert request is not None
            page, cursor = evidence_page(
                raw,
                offset=request.offset_bytes,
                start_line=request.start_line,
                line_count=request.line_count,
            )
            record = self._record(
                key,
                kind,
                reference,
                raw,
                excerpt if request.query else page,
                truncated=truncated or cursor is not None,
                next_offset_bytes=cursor,
                start_offset_bytes=(
                    request.offset_bytes
                    or sum(
                        len(x.encode())
                        for x in raw.splitlines(keepends=True)[: request.start_line - 1]
                    )
                )
                if not request.query
                else 0,
                end_offset_bytes=(
                    request.offset_bytes
                    or sum(
                        len(x.encode())
                        for x in raw.splitlines(keepends=True)[: request.start_line - 1]
                    )
                )
                + len(page.encode())
                if not request.query
                else 0,
                candidate_sha256=request.candidate_sha256,
            )
        elif decision.action == GoalAction.RETRIEVE:
            identity = decision.evidence_id
            if identity is None or not (
                any(r.record_id == identity for r in state.research)
                or identity in state.observation.artifact_ids
                or self.store.owns_worker(identity)
            ):
                raise InvestigationFailure(
                    FailureKind.PROTOCOL,
                    "Retrieval requires an evidence identity owned by this goal",
                )
            raw = self.store.original(identity)
            request = decision.inspection
            if request and (request.path or request.query):
                msg = "Retrieval paging cannot select another path or query"
                raise ValueError(msg)
            start = request.start_line if request else 1
            count = request.line_count if request else 80
            excerpt, cursor = evidence_page(
                raw,
                offset=request.offset_bytes if request else 0,
                start_line=start,
                line_count=count,
            )
            record = self._record(
                key,
                "retrieval",
                f"{identity}:L{start}+{count}",
                raw,
                excerpt,
                truncated=start > 1 or cursor is not None,
                next_offset_bytes=cursor,
                start_offset_bytes=request.offset_bytes
                if request and request.offset_bytes
                else sum(len(x.encode()) for x in raw.splitlines(keepends=True)[: start - 1]),
                end_offset_bytes=(
                    request.offset_bytes
                    if request and request.offset_bytes
                    else sum(len(x.encode()) for x in raw.splitlines(keepends=True)[: start - 1])
                )
                + len(excerpt.encode()),
            )
        elif decision.action == GoalAction.PREPARE:
            if state.observation.status in {"coding", "reviewing", "queued"}:
                msg = "Environment changes require an idle worker"
                raise ValueError(msg)
            environment = self.environment(decision.environment or "")
            raw, passed, failure, _ = self._commands(environment, state, baseline=False)
            record = self._record(
                key,
                "environment",
                environment.environment_id,
                raw,
                raw[:8000],
                environment_id=environment.environment_id,
                environment_ready=passed,
                failure=failure,
            )
        elif decision.action == GoalAction.REPRODUCE:
            if not state.environment_ready or state.observation.status in {
                "coding",
                "reviewing",
                "queued",
            }:
                msg = "Reproduction requires a prepared environment before assignment"
                raise ValueError(msg)
            environment = self.environment(state.environment_id)
            raw, passed, failure, behavior_failed = self._commands(
                environment, state, baseline=True
            )
            record = self._record(
                key,
                "baseline",
                environment.environment_id,
                raw,
                raw[:8000],
                environment_id=environment.environment_id,
                baseline_reproduced=behavior_failed
                or (passed and self.config.task.kind != "bugfix"),
                failure=failure,
            )
        elif decision.action == GoalAction.DELEGATE:
            self._task(decision.assignment, state)
            assert decision.assignment is not None
            checkpoint = ""
            manifest_sha = ""
            identity = ""
            if state.assignment is not None:
                if self.candidate is None:
                    msg_0 = "Assignment revision requires an exact preserved candidate"
                    raise ValueError(msg_0)
                root, identity = self.candidate()
                if identity != state.observation.candidate_sha256:
                    msg_0 = "Candidate changed before assignment revision"
                    raise ValueError(msg_0)
                checkpoint = "milestones/" + hashlib.sha256(key.encode()).hexdigest()[:24]
                snapshot_checkpoint(root, self.directory / checkpoint, capture(root)[0])
                experiment = state.experiments[-1] if state.experiments else None
                bound_branch = (
                    state.assignment_versions[-1].branch if state.assignment_versions else ""
                )
                if experiment and experiment.branch != bound_branch:
                    if experiment.checkpoint == self.config.campaign.revision:
                        seed_root = self.directory / "baseline"
                    else:
                        seed_root = (
                            self.directory / "verified" / experiment.checkpoint / "checkpoint"
                        )
                    if not seed_root.is_dir():
                        msg_1 = "Experiment checkpoint is unavailable"
                        raise ValueError(msg_1)
                    checkpoint += "-experiment"
                    snapshot_checkpoint(
                        seed_root, self.directory / checkpoint, capture(seed_root)[0]
                    )
                manifest_sha = hashlib.sha256(
                    (self.directory / checkpoint / "checkpoint.json").read_bytes()
                ).hexdigest()
                if self.candidate()[1] != identity:
                    msg_0 = "Candidate changed while preserving milestone"
                    raise ValueError(msg_0)
            raw = json.dumps(asdict(decision.assignment), indent=2)
            record = self._record(
                key,
                "assignment",
                "narrowed-worker-contract",
                raw,
                raw,
                assignment=decision.assignment,
                candidate_checkpoint=checkpoint,
                seed_manifest_sha256=manifest_sha,
                candidate_sha256=identity,
            )
        else:
            msg = "Unsupported investigation action"
            raise ValueError(msg)
        return self.store.put(key, intent, record, raw)

    def _record(  # noqa: PLR0913 - explicit code-owned evidence measurements
        self,
        key: str,
        kind: str,
        reference: str,
        raw: str,
        excerpt: str,
        *,
        truncated: bool = False,
        environment_id: str = "",
        environment_ready: bool = False,
        baseline_reproduced: bool = False,
        failure: FailureKind = FailureKind.NONE,
        assignment: TaskAssignment | None = None,
        next_offset_bytes: int | None = None,
        candidate_sha256: str = "",
        candidate_checkpoint: str = "",
        seed_manifest_sha256: str = "",
        start_offset_bytes: int = 0,
        end_offset_bytes: int = 0,
    ) -> ResearchRecord:
        digest = hashlib.sha256(raw.encode()).hexdigest()
        identity = (
            "e_"
            + hashlib.sha256(
                (self.config.campaign.revision + kind + reference + digest).encode()
            ).hexdigest()[:24]
        )
        # All flags come from typed code-owned measurements at the callers above.
        return ResearchRecord(
            record_id=identity,
            operation_key=key,
            kind=kind,
            revision=self.config.campaign.revision,
            reference=reference,
            content_sha256=digest,
            summary=f"{kind}: {reference}; sha256={digest}",
            excerpt=excerpt.encode()[:2000].decode("utf-8", errors="ignore"),
            truncated=truncated or len(excerpt.encode()) > 2000,
            environment_id=environment_id,
            environment_ready=environment_ready,
            baseline_reproduced=baseline_reproduced,
            failure=failure,
            assignment=assignment,
            next_offset_bytes=next_offset_bytes,
            candidate_sha256=candidate_sha256,
            candidate_checkpoint=candidate_checkpoint,
            seed_manifest_sha256=seed_manifest_sha256,
            start_offset_bytes=start_offset_bytes,
            end_offset_bytes=end_offset_bytes,
        )

    def _inspect(self, decision: SupervisorDecision) -> tuple[str, str, str, str, bool]:  # noqa: PLR0912, PLR0915 - explicit boundary dispatch/normalization
        request = decision.inspection
        assert request is not None
        config = self.config
        repository, revision = Path(config.campaign.repository), config.campaign.revision
        if request.target == "candidate":
            if self.candidate is None:
                msg = "No candidate is available"
                raise ValueError(msg)
            root, identity = self.candidate()
            if identity != request.candidate_sha256:
                msg = "Candidate reference is stale; observe the current identity"
                raise ValueError(msg)
            if (
                not request.path
                or (
                    not source_allowed(request.path)
                    and request.path not in config.task.editable_project_paths
                )
                or not within(request.path, config.campaign.scope)
            ):
                msg = "Candidate inspection requires a scoped source file"
                raise ValueError(msg)
            path = root / request.path
            if (
                path.is_symlink()
                or not path.resolve().is_relative_to(root.resolve())
                or any(parent.is_symlink() for parent in path.parents if parent != root.parent)
                or not path.is_file()
                or path.stat().st_size > MAX_ARTIFACT_BYTES
            ):
                msg = "Candidate path is absent, unsafe or exceeds text bounds"
                raise ValueError(msg)
            raw = path.read_bytes()
            if b"\x00" in raw:
                msg = "Candidate source must be UTF-8 text"
                raise ValueError(msg)
            text = raw.decode()
            if self.candidate()[1] != identity:
                msg = "Candidate changed during inspection"
                raise ValueError(msg)
            page, cursor = evidence_page(
                text,
                offset=request.offset_bytes,
                start_line=request.start_line,
                line_count=request.line_count,
            )
            return text, f"candidate:{identity}:{request.path}", "source", page, cursor is not None
        tree = local_git_bytes(repository, "ls-tree", "-r", "-z", revision)
        files: list[str] = []
        present_names: set[str] = set()
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            metadata, _, path_bytes = entry.partition(b"\t")
            name = path_bytes.decode("utf-8", errors="strict")
            present_names.add(name)
            if (
                metadata.startswith(b"100")
                and (
                    source_allowed(name)
                    and (
                        not any(p.startswith(".") for p in Path(name).parts)
                        or name in config.task.editable_project_paths
                    )
                )
                and within(name, config.campaign.scope)
            ):
                files.append(name)
        directory_files = (
            [name for name in files if name.startswith(request.path.rstrip("/") + "/")]
            if request.path
            else []
        )
        if request.path and request.path not in files and directory_files:
            selected_files = [
                name
                for name in directory_files
                if not request.query or request.query.lower() in name.lower()
            ]
            raw = json.dumps({"revision": revision, "files": selected_files}, indent=2)
            if len(raw.encode()) > MAX_ARTIFACT_BYTES:
                msg = "Directory inventory requires a narrower scoped query"
                raise ValueError(msg)
            return (
                raw,
                "inventory:" + request.path + ":" + request.query,
                "inventory",
                raw[:8000],
                len(raw) > 8000,
            )
        if request.path:
            if request.path not in files:
                if (
                    request.path in present_names
                    or not within(request.path, config.campaign.scope)
                    or not source_allowed(request.path)
                ):
                    raise InvestigationFailure(
                        FailureKind.AUTHORITY,
                        "Inspection path is outside pinned scoped regular source",
                    )
                raise InvestigationFailure(
                    FailureKind.PROTOCOL,
                    "Scoped source file is absent; inspect inventory and correct the path",
                )
            raw = local_git_bytes(repository, "show", f"{revision}:{request.path}")
            if len(raw) > MAX_ARTIFACT_BYTES or b"\x00" in raw:
                raise InvestigationFailure(
                    FailureKind.CONTEXT,
                    "Source artifact exceeds supported text bounds; narrow the inspection",
                )
            text = raw.decode("utf-8", errors="strict")
            lines = text.splitlines(keepends=True)
            selected = lines[request.start_line - 1 : request.start_line - 1 + request.line_count]
            excerpt = "".join(selected)
            if request.query:
                excerpt = "".join(
                    f"{index}: {line}"
                    for index, line in enumerate(lines, 1)
                    if request.query in line
                )[:8000]
            omitted = (
                bool(request.query)
                or request.start_line > 1
                or request.start_line - 1 + len(selected) < len(lines)
                or len(excerpt.encode()) > 8000
            )
            excerpt = excerpt.encode()[:8000].decode("utf-8", errors="ignore")
            reference = (
                f"{request.path}:L{request.start_line}+{request.line_count};query={request.query}"
            )
            return text, reference, "source", excerpt, omitted
        selected_files = [
            name for name in files if not request.query or request.query.lower() in name.lower()
        ]
        raw = json.dumps({"revision": revision, "files": selected_files}, indent=2)
        if len(raw.encode()) > MAX_ARTIFACT_BYTES:
            msg = "Repository inventory requires a narrower scoped query"
            raise ValueError(msg)
        return raw, "inventory:" + request.query, "inventory", raw[:8000], len(raw) > 8000

    def _commands(
        self, environment: ApprovedEnvironment, state: GoalState, *, baseline: bool
    ) -> tuple[str, bool, FailureKind, bool]:
        policy = state.policy
        assert policy is not None
        config = self.config
        workspace = prepare_checkout(
            Path(config.campaign.repository),
            config.campaign.revision,
            self.directory / "baseline",
            "goal/baseline",
        )
        if workspace.status().files:
            msg = "Baseline checkout changed from the pinned revision"
            raise ValueError(msg)
        checks = (
            (*environment.checks, *(probe.check() for probe in config.task.acceptance_probes))
            if baseline
            else tuple(environment.readiness)
        )
        commands = tuple(
            CommandSpec(
                name=check.name,
                argv=tuple(check.argv),
                timeout_seconds=check.timeout_seconds,
                cpu_seconds=check.timeout_seconds,
            )
            for check in checks
        )
        outcomes: list[dict[str, object]] = []
        failure = FailureKind.NONE
        with ContainerSandbox(
            workspace.root,
            config=ContainerSandboxConfig(
                image=environment.image,
                commands=commands,
                workspace_read_only=True,
                limits=coding_sandbox_limits(config.task, max_output_bytes=12000),
                environment={"PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp", "TMPDIR": "/tmp"},
            ),
        ) as sandbox:
            sandbox.capabilities.require(CODING_REQUIREMENTS)
            for check in checks:
                remaining = policy.deadline_epoch - time.time()
                if remaining <= 0:
                    msg = "Goal deadline exhausted before environment execution"
                    raise ValueError(msg)
                try:
                    result = sandbox.run(
                        check.name, timeout_seconds=min(check.timeout_seconds, remaining)
                    )
                    marker = next(
                        (
                            probe.completion_marker
                            for probe in config.task.acceptance_probes
                            if probe.name == check.name
                        ),
                        None,
                    )
                    passed = result.succeeded and (
                        marker is None or result.stdout.rstrip().endswith(marker)
                    )
                    outcome: dict[str, object] = {
                        "name": check.name,
                        "executed": True,
                        "passed": passed,
                        "exit_code": result.exit_code,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    }
                    probe = next(
                        (p for p in config.task.acceptance_probes if p.name == check.name), None
                    )
                    hint = (
                        acceptance_failure_line(probe.source, result.stdout + "\n" + result.stderr)
                        if probe is not None and not passed
                        else None
                    )
                    if hint is not None:
                        outcome["failing_acceptance_line"] = hint
                    outcomes.append(outcome)
                    if not passed and failure != FailureKind.ENVIRONMENT:
                        failure = (
                            FailureKind.VERIFICATION
                            if baseline and result.exit_code != 0
                            else FailureKind.ENVIRONMENT
                        )
                except SandboxError:
                    outcomes.append(
                        {
                            "name": check.name,
                            "executed": False,
                            "passed": False,
                            "error": "Approved sandbox execution unavailable",
                        }
                    )
                    failure = FailureKind.ENVIRONMENT
        if workspace.status().files:
            msg = "Baseline checkout changed during measurement"
            raise ValueError(msg)
        passed = all(outcome["passed"] for outcome in outcomes)
        probe_names = {probe.name for probe in config.task.acceptance_probes}
        behavior_failed = (
            baseline
            and failure == FailureKind.VERIFICATION
            and all(outcome["executed"] for outcome in outcomes)
            and any(
                outcome["name"] in probe_names and outcome.get("exit_code") != 0
                for outcome in outcomes
            )
        )
        return (
            json.dumps(
                {
                    "environment": environment.environment_id,
                    "revision": config.campaign.revision,
                    "outcomes": outcomes,
                },
                indent=2,
            ),
            passed,
            failure,
            behavior_failed,
        )
