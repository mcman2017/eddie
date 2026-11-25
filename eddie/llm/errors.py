"""Error taxonomy. Every `GuardError` is raised BEFORE any network I/O."""

from __future__ import annotations


class EddieLLMError(Exception):
    """Base class for gateway / governor errors."""


class GuardError(EddieLLMError):
    """A request was refused before any socket was opened."""


class MissingParameterError(GuardError):
    """A mandatory parameter (effort, max_output_tokens, brain_region, ...) is absent."""


class ForbiddenParameterError(GuardError):
    """A parameter value or payload key is not allowed (effort='none', tools, store=true, ...)."""


class UnsupportedModalityError(GuardError):
    """The selected backend cannot carry this content part."""


class HardInputTokenLimitError(GuardError):
    """The upper-bound input estimate exceeds the hard limit (190K) below the 3x price tier."""

    def __init__(self, estimated: int, limit: int) -> None:
        super().__init__(
            f"estimated input {estimated:,} tokens exceeds the hard limit {limit:,} "
            "(above 200,000 input tokens Ember bills the whole request at 3x)"
        )
        self.estimated = estimated
        self.limit = limit


class UnboundedInputError(GuardError):
    """The request carries parts whose token count cannot be bounded locally."""


class LiveEmberDisabledError(GuardError):
    """A live (paid) Ember call was attempted without every live guard satisfied."""


class BudgetExceededError(GuardError):
    """A reservation would cross a spend cap (kind: lifetime | daily | run | concurrency)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind} cap: {message}")
        self.kind = kind


class CircuitOpenError(GuardError):
    """The breaker is open; only `budget reset-breaker` closes it."""


class KillSwitchError(GuardError):
    """The kill-switch file exists; every Ember call is refused."""


class LedgerIntegrityError(GuardError):
    """The ledger is not the anchored canonical ledger, or is on an unsafe filesystem."""


class TransportGuardError(GuardError):
    """The wire-level transport refused to send a request."""


class UsageAnomalyError(EddieLLMError):
    """A response's usage block violated an invariant; the breaker has been tripped."""


class UpstreamError(EddieLLMError):
    """The backend answered with an error (after the request left the process)."""

    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
