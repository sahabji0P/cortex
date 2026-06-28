from fastapi import HTTPException


class AppError(HTTPException):
    """Base class for all application errors surfaced to the client."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(status_code=status_code, detail={"code": code, "message": message})
        self.code = code
        self.message = message


class NotFoundError(AppError):
    def __init__(self, resource: str) -> None:
        super().__init__(404, "not_found", f"{resource} not found")


class UnauthorizedError(AppError):
    def __init__(self, message: str = "Not authenticated") -> None:
        super().__init__(401, "unauthorized", message)


class ForbiddenError(AppError):
    def __init__(self, message: str = "Access denied") -> None:
        super().__init__(403, "forbidden", message)


class ConflictError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(409, "conflict", message)
