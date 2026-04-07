"""Exceptions for the HelloFresh UK API library."""


class HelloFreshError(Exception):
    """Base exception for HelloFresh errors."""


class AuthenticationError(HelloFreshError):
    """Raised when authentication fails."""


class CloudflareBlockError(HelloFreshError):
    """Raised when Cloudflare blocks the request (403)."""


class ApiError(HelloFreshError):
    """Raised when an API call fails."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(f"API error {status}: {message}")
