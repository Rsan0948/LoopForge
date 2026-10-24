"""The best verified candidate of a coding task, kept so a retry can resume from it.

Each verification of a new candidate records its score. The candidate with the highest
score so far is saved outside the working tree (the branch must stay at its base
revision). When a new attempt starts on a candidate that scored lower than the best,
the workspace is put back to the best one, so a later attempt's regression is not
where the next attempt begins. Scores are the harness's own verification scores;
nothing here comes from model output.
"""

import base64
from pathlib import Path

from loopforge.adapters.git_workspace import GitWorkspace
from loopforge.entrypoints.campaign_coding import patch_identity
from loopforge.entrypoints.campaign_models import StrictRecord


class Checkpoint(StrictRecord):
    identity: str
    score: float


class SavedFile(StrictRecord):
    mode: int
    data: str  # base64 bytes


class SavedCandidate(Checkpoint):
    # Changed paths relative to the base revision; None marks a deleted file.
    files: dict[str, SavedFile | None]


class CheckpointLedger(StrictRecord):
    latest: Checkpoint | None = None
    best: SavedCandidate | None = None


def _changed(workspace: GitWorkspace) -> set[str]:
    diff = workspace.run_git(
        "diff", "--name-only", "-z", "--no-renames", workspace.base_revision, "--"
    ).split("\0")
    return (set(diff) | set(workspace.status().files)) - {""}


class CheckpointStore:
    """One JSON file per task: the latest verified candidate and the best one."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> CheckpointLedger:
        if not self.path.is_file():
            return CheckpointLedger()
        return CheckpointLedger.model_validate_json(self.path.read_text())

    def _save(self, ledger: CheckpointLedger) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(ledger.model_dump_json())
        temporary.replace(self.path)

    def record(self, workspace: GitWorkspace, identity: str, score: float) -> None:
        """Note a verified candidate; save its files when it beats the best so far."""
        ledger = self.load()
        best = ledger.best
        if best is None or score > best.score:
            files: dict[str, SavedFile | None] = {}
            for name in sorted(_changed(workspace)):
                path = workspace.root / name
                files[name] = (
                    SavedFile(
                        mode=path.stat().st_mode & 0o777,
                        data=base64.b64encode(path.read_bytes()).decode(),
                    )
                    if path.is_file()
                    else None
                )
            best = SavedCandidate(identity=identity, score=score, files=files)
        self._save(CheckpointLedger(latest=Checkpoint(identity=identity, score=score), best=best))

    def restore_best(self, workspace: GitWorkspace) -> Checkpoint | None:
        """Put the workspace back to the best candidate when the current one scored lower.

        Only when the workspace is exactly the latest verified candidate: anything
        else (an operator edit, an unverified change) is left alone.
        """
        ledger = self.load()
        best, latest = ledger.best, ledger.latest
        if (
            best is None
            or latest is None
            or best.score <= latest.score
            or patch_identity(workspace) != latest.identity
        ):
            return None
        tracked = set(workspace.run_git("ls-files", "-z").split("\0")) - {""}
        for name in sorted(_changed(workspace)):
            if name in tracked:
                workspace.checkout((name,))
            else:
                (workspace.root / name).unlink(missing_ok=True)
        for name, saved in best.files.items():
            path = workspace.root / name
            if saved is None:
                path.unlink(missing_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(saved.data))
            path.chmod(saved.mode)
        if patch_identity(workspace) != best.identity:
            msg = "restored checkpoint does not match its recorded candidate"
            raise ValueError(msg)
        point = Checkpoint(identity=best.identity, score=best.score)
        self._save(CheckpointLedger(latest=point, best=best))
        return point
