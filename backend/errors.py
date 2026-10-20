"""Exceptions carrying user-facing Russian messages."""


class UserError(Exception):
    """An error whose message is safe and meaningful to show to the user (in Russian)."""

    def __init__(self, message: str, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail


class MediaError(UserError):
    pass


class Cancelled(Exception):
    """Raised when the user cancels a running job."""
