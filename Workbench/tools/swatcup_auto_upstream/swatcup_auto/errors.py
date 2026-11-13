class SwatCupAutoError(Exception):
    """Base exception for expected application errors."""


class ConfigError(SwatCupAutoError):
    """Raised when configuration is incomplete or inconsistent."""


class ProjectValidationError(SwatCupAutoError):
    """Raised when a SWAT-CUP project cannot be automated safely."""


class ParseError(SwatCupAutoError):
    """Raised when a required SWAT-CUP text output cannot be parsed."""


class BatchExecutionError(SwatCupAutoError):
    """Raised when a SWAT-CUP batch stage fails or times out."""


class CancelledError(SwatCupAutoError):
    """Raised when the user cancels a running workflow."""
