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

