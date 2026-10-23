"""Code-owned clarification and operator review records; no runtime authority grants."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ClarificationPolicy:
    topic: str
    question: str
    classification: str = "missing-information"
    timeout_seconds: int = 300
    fallback: str = ""

    def __post_init__(self) -> None:
        if (
            not self.topic
            or len(self.topic) > 80
            or not self.question.strip()
            or len(self.question) > 2000
            or self.classification not in {"optional", "missing-information", "permission"}
            or type(self.timeout_seconds) is not int
            or not 1 <= self.timeout_seconds <= 86400
            or len(self.fallback) > 2000
            or (self.classification != "optional" and self.fallback)
            or (self.classification == "optional" and not self.fallback.strip())
        ):
            msg = "Invalid clarification policy; silence cannot grant permission"
            raise ValueError(msg)


@dataclass(frozen=True)
class ClarificationRequest:
    topic: str
    category: str
    question: str

    def __post_init__(self) -> None:
        if (
            not self.topic
            or len(self.topic) > 80
            or not self.category
            or len(self.category) > 80
            or not self.question.strip()
            or len(self.question) > 2000
        ):
            msg = "Invalid dynamic clarification request"
            raise ValueError(msg)


@dataclass(frozen=True)
class QuestionOpened:
    topic: str
    deadline_epoch: int
    question: str = ""
    category: str = ""


@dataclass(frozen=True)
class QuestionAnswered:
    topic: str
    answer: str
    answered_epoch: int


@dataclass(frozen=True)
class ExperimentRecorded:
    topic: str
    branch: str
    checkpoint: str
    recorded_epoch: int


@dataclass(frozen=True)
class ControlRequested:
    command: str


@dataclass(frozen=True)
class ControlAcknowledged:
    command: str


@dataclass(frozen=True)
class ReviewRecorded:
    disposition: str
    candidate_sha256: str
    commit: str = ""


@dataclass(frozen=True)
class QuestionState:
    topic: str
    deadline_epoch: int
    answer: str = ""
    answered_epoch: int = 0


@dataclass(frozen=True)
class ExperimentState:
    topic: str
    branch: str
    checkpoint: str
    assumption: str


ReviewEvent = (
    QuestionOpened
    | QuestionAnswered
    | ExperimentRecorded
    | ControlRequested
    | ControlAcknowledged
    | ReviewRecorded
)
