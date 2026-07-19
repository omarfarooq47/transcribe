class AppError(Exception):
    """Base application error."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class ValidationError(AppError):
    """Invalid client input (maps to HTTP 400)."""


class NotFoundError(AppError):
    """Resource not found (maps to HTTP 404)."""


class ConflictError(AppError):
    """Invalid state transition (maps to HTTP 409)."""


class PermanentTranscriptionError(AppError):
    """Non-retryable transcription failure."""


class TransientTranscriptionError(AppError):
    """Retryable transcription failure."""
