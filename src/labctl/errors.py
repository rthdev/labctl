"""Stable application errors and process statuses."""

from enum import IntEnum


class ExitStatus(IntEnum):
    """Stable process exit statuses."""

    SUCCESS = 0
    USAGE = 2
    NOT_FOUND = 3
    CONFLICT = 4
    PREREQUISITE = 5
    OPERATION = 6
    GRADING = 7


class LabctlError(Exception):
    """Expected user-facing error."""

    def __init__(self, message: str, status: ExitStatus = ExitStatus.OPERATION) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
