"""Code-owned measurements for admission of a complete provider request."""

from dataclasses import dataclass
from enum import StrEnum


class InputMeasurementBasis(StrEnum):
    UTF8_BYTE_UPPER_BOUND = "utf8_byte_upper_bound"
    TOKEN_COUNTER = "token_counter"


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextAdmission:
    measurement_basis: InputMeasurementBasis
    input_token_bound: int
    serialized_request_bytes: int
    context_window_tokens: int
    output_reserve_tokens: int
    framing_reserve_tokens: int
    compacted_turns: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.measurement_basis, InputMeasurementBasis):  # pyright: ignore[reportUnnecessaryIsInstance]
            msg = "context measurement basis must be code-owned"
            raise TypeError(msg)
        for value in (
            self.input_token_bound,
            self.serialized_request_bytes,
            self.context_window_tokens,
            self.output_reserve_tokens,
            self.framing_reserve_tokens,
            self.compacted_turns,
        ):
            if type(value) is not int or value < 0:
                msg = "context admission counts must be nonnegative integers"
                raise ValueError(msg)
        if self.context_window_tokens == 0:
            msg = "context window must be positive"
            raise ValueError(msg)

    @property
    def admitted(self) -> bool:
        return (
            self.input_token_bound + self.output_reserve_tokens + self.framing_reserve_tokens
            <= self.context_window_tokens
        )
