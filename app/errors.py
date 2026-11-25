"""Domain errors. The API layer maps each one to an HTTP status; services never import HTTP concepts."""


class AuthError(Exception):          # 401: missing/invalid credentials
    pass


class Forbidden(Exception):          # 403: authenticated but not allowed
    pass


class NotFound(Exception):           # 404
    pass


class Conflict(Exception):           # 409
    pass


class RateLimited(Exception):        # 429
    def __init__(self, message: str, retry_after: int = 60, limit: int = 0):
        super().__init__(message)
        self.retry_after, self.limit = retry_after, limit
