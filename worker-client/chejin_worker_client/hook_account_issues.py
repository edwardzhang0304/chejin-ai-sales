"""Allowlisted account admission reasons, shared by receipt checks and UI."""
ACCOUNT_ISSUES = {
    'HOOK_SALES_DISABLED': ('销售账号已停用', '请联系管理员。'),
    'HOOK_WORKER_DISABLED': ('Worker 已停用', '请联系管理员。'),
    'HOOK_SALES_BINDING_REQUIRED': ('销售绑定尚未确认', '请联系管理员核对销售绑定和启用状态。'),
    'HOOK_CLIENT_BINDING_REQUIRED': ('客户端绑定尚未确认', '请联系管理员核对客户端绑定。'),
    'HOOK_SALES_BINDING_CHANGED': ('销售绑定已变化', '请联系管理员核对原绑定后处理。'),
    'HOOK_ACCOUNT_CHANGED': ('工作微信账号与原绑定不一致', '请联系管理员核对账号及原绑定。'),
    'HOOK_ACCOUNT_EPOCH_MISMATCH': ('工作微信绑定凭证不一致', '请联系管理员核对原绑定凭证。'),
    'HOOK_ACCOUNT_ALREADY_BOUND': ('工作微信已绑定其他 Worker', '请联系管理员核对账号归属。'),
    'HOOK_CONTEXT_REQUIRED': ('工作微信账号信息尚未确认', '请联系管理员检查客户端与后台的配套配置。'),
    'HOOK_LOGIN_REQUIRED': ('工作微信尚未登录', '请先登录原工作微信，客户端会重新检查。'),
}


def account_block_code(reason):
    # Unknown/legacy reasons must never be interpreted as disabled or permitted.
    return reason if isinstance(reason, str) and reason in ACCOUNT_ISSUES else 'HOOK_ACCOUNT_NOT_CONFIRMED'


class HookAccountConfirmationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)
