from enum import StrEnum


class LeadStatus(StrEnum):
    unassigned = "unassigned"
    assigned = "assigned"
    invalid = "invalid"


class AssignStatus(StrEnum):
    unassigned = "unassigned"
    assigned = "assigned"
    assign_failed = "assign_failed"


class ContactType(StrEnum):
    phone = "phone"
    wechat = "wechat"
    email = "email"


class AssignmentType(StrEnum):
    round_robin = "round_robin"
    retry_round_robin = "retry_round_robin"


class AssignmentResult(StrEnum):
    succeeded = "succeeded"
    failed = "failed"


class NoteType(StrEnum):
    manual = "manual"
    duplicate_append = "duplicate_append"
    system = "system"


class TaskType(StrEnum):
    add_friend = "add_friend"
    chat_reply = "chat_reply"


class TaskStatus(StrEnum):
    blocked = "blocked"
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class TaskResultCode(StrEnum):
    invite_sent = "invite_sent"
    already_friend = "already_friend"
    chat_reply_sent = "chat_reply_sent"
    skipped_by_rule = "skipped_by_rule"
    manual_closed = "manual_closed"


class TaskErrorCode(StrEnum):
    WORKER_OFFLINE = "WORKER_OFFLINE"
    WORKER_UI_LOCK_TIMEOUT = "WORKER_UI_LOCK_TIMEOUT"
    RPA_WECHAT_WINDOW_NOT_FOUND = "RPA_WECHAT_WINDOW_NOT_FOUND"
    RPA_PHONE_SEARCH_FAILED = "RPA_PHONE_SEARCH_FAILED"
    RPA_ADD_FRIEND_BUTTON_NOT_FOUND = "RPA_ADD_FRIEND_BUTTON_NOT_FOUND"
    RPA_REMARK_WRITE_FAILED = "RPA_REMARK_WRITE_FAILED"
    RPA_SEND_INVITE_FAILED = "RPA_SEND_INVITE_FAILED"
    WECHAT_LOGIN_EXPIRED = "WECHAT_LOGIN_EXPIRED"
    WECHAT_RISK_PROMPT_DETECTED = "WECHAT_RISK_PROMPT_DETECTED"
    TASK_DUPLICATE_SEND_BLOCKED = "TASK_DUPLICATE_SEND_BLOCKED"


class TaskBlockCode(StrEnum):
    SALES_WORKER_NOT_BOUND = "SALES_WORKER_NOT_BOUND"
    DAILY_LIMIT_REACHED = "DAILY_LIMIT_REACHED"


class TaskEventType(StrEnum):
    created = "created"
    blocked = "blocked"
    unblocked = "unblocked"
    claimed = "claimed"
    step_updated = "step_updated"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    comment_added = "comment_added"


class InvalidReason(StrEnum):
    empty_number = "empty_number"
    wrong_info = "wrong_info"
    not_target_customer = "not_target_customer"
    test_data = "test_data"
    duplicate_or_mistaken = "duplicate_or_mistaken"
    other = "other"


INVALID_REASON_LABELS: dict[InvalidReason, str] = {
    InvalidReason.empty_number: "空号",
    InvalidReason.wrong_info: "信息错误",
    InvalidReason.not_target_customer: "非目标客户",
    InvalidReason.test_data: "测试数据",
    InvalidReason.duplicate_or_mistaken: "重复/误录",
    InvalidReason.other: "其他",
}
