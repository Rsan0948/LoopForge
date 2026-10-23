"""All paid roles share this code-owned authority boundary."""

from typing import Protocol

from loopforge.domain.paid_budget import PaidAccount


class PaidBudgetPort(Protocol):
    def reserve(self, key: str, role: str, maximum: int) -> None: ...
    def settle(self, key: str, charged: int) -> None: ...
    def account(self) -> PaidAccount: ...
