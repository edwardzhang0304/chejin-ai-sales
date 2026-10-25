"""Sales-facing projections of existing checks; no active probe or auto-resume."""
ISSUES = {
 'HOOK_STARTUP_WINDOW_OBSERVATION_FAILED': ('微信窗口状态暂时无法确认', '客户端暂时无法读取微信窗口状态，已暂停准备操作；仍未恢复时请导出故障证据。'),
 'HOOK_STARTUP_FOREGROUND_REQUIRED': ('请将微信主窗口切到前台', '点击工作微信主窗口并保持在前台，客户端会继续校验；不会自动开始接单。'),
 'HOOK_STARTUP_RETRY_WAIT': ('等待再次校验微信窗口', '启动检查未完成，稍后在微信主窗口处于前台时重新校验；不会反复抢占前台。'),
 'HOOK_STARTUP_RETRY_EXHAUSTED': ('请重开 Worker 校验微信窗口', '本次启动已连续检查失败三次。请保留数据和微信登录，关闭微信弹窗后重开 Worker；原任务不会重新提交。'),
 'HOOK_STARTUP_FRAME_EXPIRED': ('启动画面校验超时', '截图到校验完成超过有效期，未使用过期画面；请保持微信主窗口在前台等待重新校验。'),
 'HOOK_STARTUP_DPI_UNVERIFIED': ('屏幕缩放检查未通过', '客户端无法确认屏幕缩放状态，暂不操作微信，请导出故障证据。'),
 'HOOK_SEARCH_LAYOUT_RESTART_REQUIRED': ('请重开 Worker 校验微信位置', '启动定位已失效。请关闭并重新打开 Worker，保留原数据和微信登录；原任务仍按记录核对，不会重新提交申请。'),
 'HOOK_C1_POPUP_PENDING': ('请先关闭微信弹窗', '微信仍有未关闭的窗口。请核对并取消遗留申请窗口，关闭其他弹窗后再恢复接单；程序不会自动提交。'),
 'HOOK_STARTUP_WINDOW_PENDING': ('等待微信窗口初始化', '先确认上次流程结果，再调整微信窗口；完成前不执行新任务。'),
 'HOOK_C1_STARTUP_WINDOW_UNVERIFIED': ('微信窗口调整未通过', '请保持微信主窗口打开，关闭微信弹窗后重新检查；暂不执行新任务。'),
 'HOOK_PROCESS_MISSING_OR_AMBIGUOUS': ('未找到工作微信', '请打开指定的工作微信并登录；不要同时运行多个测试微信。'),
 'HOOK_PROCESS_PATH_MISMATCH': ('工作微信路径不匹配', '请使用配置中指定的微信程序。'),
 'HOOK_COMPONENT_VERSION_MISMATCH': ('微信版本检查未通过', '请使用配套的微信 3.9.10.16 和 Hook 组件。'),
 'HOOK_COMPONENT_HASH_OR_LOAD_MISMATCH': ('Hook 组件检查未通过', '组件未加载或与配置不匹配，请按原启动流程打开工作微信。'),
 'HOOK_PROCESS_ARGUMENTS_MISMATCH': ('微信启动方式检查未通过', '请按原启动流程打开工作微信。'),
 'HOOK_API_PORT_OWNER_MISMATCH': ('Hook 接口检查未通过', '接口未就绪或被其他程序占用，请检查工作微信的启动状态。'),
 'HOOK_ACCOUNT_NOT_CONFIRMED': ('工作微信账号核验未通过', '当前账号尚未确认或与销售绑定不一致，请检查账号及后台绑定。'),
 'HOOK_CALLBACK_ACCOUNT_MISMATCH': ('工作微信账号不一致', '请登录原绑定的工作微信；换号需要通过后台重新绑定。'),
 'HOOK_CALLBACK_ACCOUNT_CHANGED': ('工作微信账号已变化', '请检查当前账号，并通过后台重新绑定后再接单。'),
 'HOOK_CALLBACK_REGISTRATION_UNCONFIRMED': ('消息接收配置检查未通过', '请检查工作微信和消息接收服务；暂时不要接单。'),
 'HOOK_RECEIVER_START_FAILED': ('消息接收服务启动失败', '请关闭旧接收程序，并检查接收端口是否被占用。'),
 'HOOK_RECEIVER_STOP_UNCONFIRMED': ('消息接收服务尚未安全关闭', '请等待记录保存结束，暂时不要重复启动客户端。'),
 'HOOK_LOCAL_STORAGE_FAILED': ('本机记录读写失败，已停止接单', '请检查磁盘空间、记录目录权限及文件占用；处理后重启 Worker，原任务按记录恢复。'),
 'HOOK_MESSAGE_SAVE_FAILED': ('消息保存检查未通过', '请检查磁盘空间和本机记录目录的写入权限。'),
 'HOOK_HISTORY_STORAGE_CAPACITY_EXCEEDED': ('本机记录容量不足', '已停止处理新工作，请先释放空间或核对到期记录；未完成的记录仍保留。'),
 'HOOK_BACKEND_CLOCK_STALE': ('等待后台时间校验', '后台时间记录已过期，重连后会重新校验；期间暂不执行新操作。'),
 'HOOK_BACKEND_CLOCK_RTT_UNCONFIRMED': ('等待后台连接稳定', '后台响应较慢，暂时无法核验操作时间；连接稳定后会重新检查。'),
 'HOOK_BACKEND_CLOCK_UNCONFIRMED': ('后台时间检查未通过', '后台时间记录无法核验，请检查后台服务后按原故障恢复流程处理。'),
 'HOOK_BACKEND_CLOCK_CHANGED': ('后台时间发生异常变化', '已停止接单，请检查后台时间后按原故障恢复流程处理。'),
 'HOOK_BACKEND_TEMPORARILY_UNAVAILABLE': ('等待后台恢复连接', '后台服务暂时无法响应，恢复后会重新核验；期间暂不执行新操作。'),
 'HOOK_ACCOUNT_REQUEST_REJECTED': ('账号核验未通过', '请检查客户端与后台的绑定，核验通过后按原恢复流程继续。'),
 'HOOK_BACKEND_NETWORK_FAILED': ('后台连接检查未通过', '请检查网络和后台服务，连接恢复后会重新检查。'),
 'HOOK_LOCAL_TRANSPORT_FAILED': ('Hook 接口连接异常', '请检查工作微信；未知调用结果不会自动重试。'),
 'HOOK_PROCESS_ACTIVE_CALL_QUARANTINED': ('微信接口上次调用尚未确认', '已停止接单，请按故障恢复流程处理，避免重复操作。'),
 'HOOK_PROCESS_INSPECTION_FAILED': ('工作微信运行检查未通过', '请检查指定微信是否正常运行，以及客户端的访问权限。'),
 'HOOK_CONNECTION_CHECK_FAILED': ('工作微信连接检查未通过', '请检查微信运行状态；具体记录可在本机日志中查看。'),
 'HOOK_C1_OCR_RUNTIME_UNAVAILABLE': ('OCR 识别组件不可用', '请检查客户端运行环境，恢复识别组件后再接单。'),
 'HOOK_WINDOW_UNAVAILABLE': ('微信窗口不可操作', '请打开微信主窗口，恢复窗口后再接单。'),
 'HOOK_NATIVE_AUTO_TEXT_UNCONFIRMED': ('语音设置检查未通过', '请关闭微信语音自动转文字，重新打开客户端并确认准备完成。'),
 'HOOK_NOT_LOGGED_IN': ('工作微信尚未登录', '请先登录工作微信，客户端会重新检查。'),
 'WORKER_AUTOMATION_UNAVAILABLE': ('自动化组件检查未通过', '请检查客户端运行环境，恢复后再接单。'),
 'WORKER_WECHAT_DISCONNECTED': ('工作微信未连接', '请打开并登录工作微信，客户端会重新检查。'),
}
ISSUES.update({
 'HOOK_RECEIVER_NOT_READY': ('消息接收服务尚未就绪', '请等待接收服务启动，仍未恢复时查看本机日志。'),
 'HOOK_RECEIVER_STOPPED': ('消息接收服务已停止', '已停止接单，请检查接收服务并按原故障恢复流程处理。'),
 'HOOK_LOGIN_RESPONSE_INVALID': ('微信登录状态检查未通过', '接口返回的登录状态无法核验，请检查工作微信和配套组件。'),
 'HOOK_LOGIN_IDENTITY_MISSING': ('工作微信账号无法识别', '请确认微信已登录，并检查配套 Hook 组件。'),
 'HOOK_LOCAL_RESPONSE_INVALID': ('Hook 接口返回异常', '返回内容无法核验，请检查工作微信和配套组件。'),
 'HOOK_HTTP_RESPONSE_REJECTED': ('Hook 接口检查未通过', '接口未返回可用结果，请检查工作微信的运行状态。'),
 'HOOK_PROCESS_GENERATION_CHANGED': ('工作微信进程已变化', '请等待客户端重新核验微信；原任务不会自动重做。'),
})


def checking():
    return {'state':'checking','title':'正在检查微信环境',
            'message':'','canStart':False}


def passed():
    return {'state':'passed','title':'已暂停接单','message':'','canStart':True}


def failed(code,*,operation=None,transport_kind=None):
    title,message=ISSUES.get(code,('启动检查未通过','请查看本机日志中的具体原因，处理后再接单。'))
    if code=='HOOK_LOCAL_TRANSPORT_FAILED' and operation=='Voice2Text' and transport_kind=='timeout':
        title,message='语音转写等待超时','未取得确定结果，已停止接单；原语音不会自动重试。'
    return {'state':'failed','title':title,'message':message,'canStart':False}

ISSUES.update({'HOOK_MODEL_OBSERVATION_UNSUPPORTED':('后台能力检查未通过','后台版本不支持暂停期间只查看结果，请使用配套后台；期间暂不接单。'), 'HOOK_MODEL_OBSERVATION_UNCONFIRMED':('后台结果查询检查未通过','后台未确认只查看结果，已停止接单；请检查配套后台。')})
