class IdempotencyConflictError(ValueError):
    pass


class InvalidTransitionError(ValueError):
    pass


class InvalidLeaseError(ValueError):
    pass


class NotFoundError(KeyError):
    pass


class CorpusValidationError(ValueError):
    pass


class RevisionConflictError(ValueError):
    pass


class DraftValidationError(ValueError):
    """Publishing was refused because the draft still violates the golden contract."""

    def __init__(self, issues: list[dict]) -> None:
        self.issues = issues
        super().__init__(f"draft_invalid: {len(issues)} issue(s)")
