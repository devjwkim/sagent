class SagentError(Exception):
    """Base error for core operations; message is safe to show to users."""


class NotFound(SagentError):
    pass


class Forbidden(SagentError):
    pass


class ValidationError(SagentError):
    pass


class Conflict(SagentError):
    pass
