class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.data = data or {}


class DuplicateLeadError(AppError):
    def __init__(self, message: str, data: dict):
        super().__init__("LEAD_PHONE_DUPLICATED", message, 409, data)

