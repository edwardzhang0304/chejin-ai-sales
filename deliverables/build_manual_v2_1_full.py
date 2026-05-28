from __future__ import annotations

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

from build_delivery_artifacts import PROJECT_NAME, ROOT, UNIT_PRICE, bullet_list, make_table, p


DOC_VERSION = "v2.3"
DOC_DATE = "2026-05-25"
PDF_PATH = ROOT / f"{PROJECT_NAME}_技术方案手册_{DOC_VERSION}_详细设计全量版.pdf"


def make_styles():
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "CNBodyV21",
        parent=styles["BodyText"],
        fontName="STSong-Light",
        fontSize=8.6,
        leading=12.1,
        spaceAfter=3.2,
        textColor=colors.HexColor("#1F2937"),
    )
    small = ParagraphStyle(
        "CNSmallV21",
        parent=body,
        fontSize=7.2,
        leading=9.5,
        spaceAfter=2,
    )
    title = ParagraphStyle(
        "CNTitleV21",
        parent=styles["Title"],
        fontName="STSong-Light",
        fontSize=21,
        leading=27,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#0B2545"),
        spaceAfter=9,
    )
    subtitle = ParagraphStyle(
        "CNSubtitleV21",
        parent=body,
        fontSize=10.5,
        leading=15,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#4B5563"),
        spaceAfter=10,
    )
    h1 = ParagraphStyle(
        "CNH1V21",
        parent=styles["Heading1"],
        fontName="STSong-Light",
        fontSize=13.2,
        leading=16.5,
        textColor=colors.HexColor("#1F4E78"),
        spaceBefore=7,
        spaceAfter=4,
    )
    h2 = ParagraphStyle(
        "CNH2V21",
        parent=styles["Heading2"],
        fontName="STSong-Light",
        fontSize=10.4,
        leading=13.2,
        textColor=colors.HexColor("#2E74B5"),
        spaceBefore=4,
        spaceAfter=2,
    )
    code = ParagraphStyle(
        "CNCodeV21",
        parent=body,
        fontName="STSong-Light",
        fontSize=7.4,
        leading=9.6,
        backColor=colors.HexColor("#F4F6F9"),
        borderColor=colors.HexColor("#E5E7EB"),
        borderWidth=0.3,
        borderPadding=4,
        spaceBefore=2,
        spaceAfter=4,
    )
    return body, small, title, subtitle, h1, h2, code


def table(data, widths, style, repeat=1, font_size=7.2):
    return make_table([[p(str(c), style) for c in row] for row in data], widths, repeat=repeat, font_size=font_size)


def section(story, title_text, h1):
    story.append(p(title_text, h1))


def sub(story, title_text, h2):
    story.append(p(title_text, h2))


def bullets(story, items, style):
    story.append(bullet_list(items, style))


def codeblock(story, text, code):
    story.append(p(text, code))


def decision(story, rows, small):
    story.append(table([["事项", "口径"]] + rows, [43 * mm, 115 * mm], small))


def generate_pdf() -> None:
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    body, small, title, subtitle, h1, h2, code = make_styles()
    doc = SimpleDocTemplate(
        str(PDF_PATH),
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"{PROJECT_NAME} 技术方案手册 {DOC_VERSION}",
    )

    story = []
    story.append(Spacer(1, 11 * mm))
    story.append(p(PROJECT_NAME, title))
    story.append(p("技术方案手册（详细设计全量版）", subtitle))
    story.append(table([
        ["文档版本", DOC_VERSION],
        ["文档日期", DOC_DATE],
        ["预算基线", f"87,000元（58人天 × {UNIT_PRICE}元/人天）"],
        ["文档用途", "软件项目标书式详细设计、研发拆解、验收用例和范围变更基线"],
        ["交付核心", "能正常回复、按规则自动召回、无法安全回复时触发人工接管；性能指标仅作为优化目标"],
    ], [34 * mm, 124 * mm], body, repeat=0, font_size=8.8))
    story.append(Spacer(1, 5 * mm))
    story.append(p("本版在不缩减一期范围的前提下，将风险评估中成立的问题转化为明确工程约束：状态机、幂等、防重发、Worker兜底、外部接口Gate、RAG/Guard验收、飞书通知日志和测试分级。后续可继续修订，但本版先保证模块、边界、状态、异常、验收和待确认事项不遗漏。", body))
    story.append(PageBreak())

    section(story, "1. 总体架构与交付原则", h1)
    bullets(story, [
        "系统本质是“线索到销售接管”的售前跟进系统，不是通用聊天机器人。",
        "云端业务控制面负责状态、任务、配置、AI、RAG、车源、风控、飞书通知和审计。",
        "商家侧Worker负责微信桌面端操作、消息采集、图片另存、回复发送和本地可视化执行台。",
        "销售人工接管发生在销售微信手机客户端；Worker控制商家侧电脑上同一销售微信号登录的微信桌面客户端。",
        "大模型与视觉模型调用均放服务端；Worker不持有模型API Key、大风车密钥、飞书密钥和完整知识库。",
        "转人工不修改微信备注；接管状态以系统内部状态为准，通过飞书机器人通知销售手机端。",
        "任何断网、重启、超时、恢复场景，不得重复发送同一条AI回复或召回文案。",
    ], body)
    story.append(table([
        ["场景", "状态", "动作"],
        ["普通聊天达到20条AI回复上限", "watching", "AI停止，不触发飞书，不关闭线索，可进入召回候选"],
        ["高风险/高意向/接管关键词", "handoff_required", "AI停止，飞书机器人通知销售"],
        ["销售手机端人工回复", "human_active", "AI停止，后续客户消息不再自动回复，不再召回"],
        ["客户明确拒绝", "rejected", "不自动处理，后续重复导入也不自动加好友/回复/召回"],
        ["人工确认结束", "closed", "线索关闭，不再进入自动流程"],
    ], [42 * mm, 35 * mm, 81 * mm], small))
    sub(story, "1.1 一期范围不变下的风险消化原则", h2)
    story.append(table([
        ["风险", "本版处理口径"],
        ["Worker控制微信脆弱", "承认为一期最大技术风险；要求微信版本锁定、兼容性矩阵、UI锁租约、看门狗、截图留痕、失败暂停和人工降级。"],
        ["预算与范围矛盾", "一期范围不缩减，但将验收分为硬交付、优化目标和外部依赖项；未联调外部接口不得视为内部开发失败。"],
        ["状态流转模糊", "增加状态机矩阵，任何自动动作必须校验当前状态和允许动作。"],
        ["重复发送风险", "以服务端幂等为准，Worker只能执行最新有效action；数据库唯一约束和事务顺序优先于本地缓存。"],
        ["大风车接口不确定", "设为Gate 0前置确认项；接口不足时走本地导入/索引降级，不影响文字主链路验收。"],
        ["模型/RAG不稳定", "低置信、模型失败、证据不足均转人工；不使用兜底话术冒险自动回复。"],
    ], [42 * mm, 116 * mm], small))
    sub(story, "1.2 主状态机矩阵", h2)
    story.append(table([
        ["当前状态", "允许进入", "允许自动动作", "退出条件", "禁止动作"],
        ["new", "线索入库", "分配销售", "assigned", "自动聊天、召回"],
        ["assigned", "销售/Worker绑定完成", "创建add_friend任务", "add_friend_pending", "自动聊天、召回"],
        ["add_friend_pending", "任务创建", "Worker加好友", "add_friend_sent、friend_added、failed", "重复创建同手机号未完成任务"],
        ["add_friend_sent", "申请已提交", "等待/人工确认通过后绑定", "friend_added、failed、rejected", "未绑定前自动回复"],
        ["friend_added", "已是好友或通过申请", "绑定会话、进入监听", "ai_chatting、watching", "未绑定会话时发送AI回复"],
        ["ai_chatting", "会话已绑定且AI开启", "文字/图片自动回复、风险判定", "watching、handoff_required、human_active、rejected", "绕过Guard发送"],
        ["watching", "观望、20条上限、人工标记", "满足规则后创建follow_up", "ai_chatting、handoff_required、human_active、rejected、closed", "无规则直接连续召回"],
        ["handoff_required", "风险/高意向/模型失败", "飞书通知、停止AI", "human_active、closed", "继续AI自动回复、自动召回"],
        ["human_active", "销售人工回复或人工确认接管", "记录人工状态", "closed、watching需人工确认", "AI自动回复、自动召回"],
        ["rejected", "客户明确拒绝/黑名单", "拒绝后续自动处理", "仅人工解除", "自动加好友、自动回复、自动召回"],
        ["closed", "人工关闭", "归档", "人工重开", "自动动作"],
    ], [26 * mm, 34 * mm, 35 * mm, 37 * mm, 26 * mm], small, font_size=6.3))
    sub(story, "1.3 交付分类", h2)
    story.append(table([
        ["类别", "说明", "示例"],
        ["硬交付", "必须能完成，否则影响验收。", "线索入库、Worker执行台、加好友、文字/图片回复或转人工、防重复发送、飞书通知、召回任务。"],
        ["优化目标", "作为设计目标，不作为未经压测的绝对承诺。", "文字回复P95、图片回复P95、后台查询P95、RAG命中率。"],
        ["外部依赖", "需对方或真实环境提供条件；未满足时按降级路径验收。", "大风车API权限、微信版本/账号状态、飞书机器人配置、模型服务可用性。"],
    ], [28 * mm, 58 * mm, 72 * mm], small))

    section(story, "2. 模块1：云端业务控制面", h1)
    sub(story, "2.1 模块目标", h2)
    bullets(story, [
        "作为业务状态中心、任务调度中心、配置中心和审计中心。",
        "统一管理线索、销售、Worker、任务、会话、风控、召回、飞书通知和日志。",
        "第一期做轻量后台，由项目方自己控制，不做复杂权限和复杂CRM。",
    ], body)
    sub(story, "2.2 不做事项", h2)
    bullets(story, [
        "不做多商户计费、复杂权限、完整CRM、复杂BI、多渠道客服聚合、销售业绩管理、合同订单系统、高可用集群。",
        "本方案不覆盖二期SaaS化、多商户、复杂权限和计费体系设计；相关内容仅作为未来扩展边界，不作为本次开发、测试和验收范围。",
        "不直接操作微信UI，不保存Worker本地临时状态为主状态。",
    ], body)
    sub(story, "2.3 核心子模块与对象", h2)
    story.append(table([
        ["子模块/对象", "说明"],
        ["Lead", "手机号线索，包含phone、phone_hash、source、sales_id、worker_id、status、remark_code、last_contact_at、reject_flag、recall_count等。"],
        ["Sales", "销售人员，包含sales_id、sales_name、wechat_account、worker_id、feishu_user_id、enabled、daily_add_friend_limit等。"],
        ["Worker", "商家侧电脑执行器，包含worker_id、device_name、sales_id、wechat_status、last_heartbeat_at、current_task_type等。"],
        ["Task", "统一任务表，task_type包含add_friend、chat_reply、follow_up，状态包含pending、running、succeeded、failed、skipped、cancelled。"],
        ["Conversation", "微信会话业务状态，包含lead_id、sales_id、ai_enabled、status、reply_count、handoff_reason等。"],
        ["RiskPolicy", "风控策略配置。"],
        ["HandoffEvent", "人工接管事件，记录接管原因、通知结果和错误信息。"],
    ], [36 * mm, 122 * mm], small))
    sub(story, "2.4 核心流程", h2)
    codeblock(story, "线索入库 -> 分配销售 -> 绑定Worker -> 创建add_friend任务 -> Worker执行 -> 回传结果 -> 更新线索/任务状态", code)
    codeblock(story, "Worker上报客户消息 -> 控制面检查会话和风控 -> AI/RAG/Guard -> 返回send_reply/handoff/no_action/pause -> Worker执行 -> 审计", code)
    codeblock(story, "watching客户每日扫描 -> 满足N天未联系且未拒绝 -> 创建follow_up任务 -> Worker发送固定文案 -> 记录结果", code)
    sub(story, "2.5 已确认决策与待确认", h2)
    decision(story, [
        ["登录权限", "第一期轻量后台，项目方自控，不做复杂权限。"],
        ["销售Worker关系", "一对一绑定，允许换绑，换绑需留痕。"],
        ["分配策略", "待定，预留手动分配和轮询分配。"],
        ["飞书通知", "只做飞书，定向销售个人；不做短信。"],
        ["召回规则", "第一期一种规则，周期可配置，默认待定。"],
        ["验收重点", "状态准确、任务可追踪、配置可调整、失败原因可见。"],
    ], small)

    section(story, "3. 模块2：Worker任务类型与本地执行台", h1)
    sub(story, "3.1 模块目标与形态", h2)
    bullets(story, [
        "Worker部署在商家侧Windows电脑，负责看微信、点微信、读消息、存图片、发回复。",
        "Worker执行台必须呈现为本地可视化窗口，交互效果参照附件视频：微信桌面客户端旁边展示任务步骤、截图证据、AI结果、运行状态和控制按钮。",
        "Worker不保存业务主状态，不直接调用大模型，不持有模型、飞书、大风车密钥。",
        "Worker不需要开机自启，通过执行台启动按钮操作。",
    ], body)
    story.append(table([
        ["任务类型", "职责", "不负责"],
        ["add_friend", "手机号搜索、发送好友申请、写初始绑定备注、回传申请结果", "不聊天、不调用AI、不判断意向"],
        ["chat_reply", "监听已绑定会话、读取文字/图片、另存图片、上传服务端、执行服务端动作", "不批量加好友、不做线索分配、不改转人工备注"],
        ["follow_up", "领取召回任务、发送固定召回文案、上报结果", "不判断召回资格、不AI自由生成文案"],
        ["Local WeChat UI Lock", "所有微信桌面端UI操作串行化", "不决定业务状态"],
    ], [30 * mm, 73 * mm, 55 * mm], small))
    sub(story, "3.2 UI锁、优先级与恢复", h2)
    bullets(story, [
        "所有点击微信、输入微信、发送微信、切换窗口、写备注、另存图片动作必须先获取Local WeChat UI Lock。",
        "上传消息、等待AI、图片识别、车源查询、日志记录等非UI动作不占用UI锁。",
        "任务优先级为chat_reply大于add_friend大于follow_up；add_friend优先follow_up。",
        "chat_reply等待服务端AI结果时不占用UI锁，add_friend可以先执行。",
        "Worker重启后读取本地快照并向服务端确认，以服务端状态为准恢复。",
        "pending_action恢复时必须检查reply_action_id未发送、未过期、会话仍允许回复、未人工接管、客户没有新消息覆盖上下文。",
    ], body)
    story.append(table([
        ["控制项", "工程要求"],
        ["锁租约", "UI锁必须有lease_expires_at；Worker异常退出或超过租约未续约时，服务端可释放并标记任务为recovering。"],
        ["锁超时", "单个UI步骤必须配置step_timeout；超过后截图、记录窗口标题、释放锁并进入可重试或人工处理。"],
        ["看门狗", "Worker每隔固定周期上报heartbeat、当前任务、当前步骤、微信窗口状态；控制面展示离线和卡住状态。"],
        ["队列压力", "chat_reply队列积压时暂停低优先级follow_up，必要时暂停add_friend，优先保证已对话客户不失控。"],
        ["版本锁定", "交付环境需记录Windows版本、微信桌面端版本、Worker版本；微信升级前必须回归核心用例。"],
        ["人工降级", "Worker连续失败或微信风险提示时自动暂停该Worker，后台允许导出待处理任务给人工处理。"],
    ], [35 * mm, 123 * mm], small))
    sub(story, "3.3 不重复发送硬约束", h2)
    codeblock(story, "message_id/dedupe_key用于识别同一条客户消息；reply_action_id用于识别同一次服务端回复动作；sent_ack用于确认Worker已发送。已sent、已过期、上下文变化或已人工接管的动作不得补发。", code)
    story.append(table([
        ["对象", "唯一约束/幂等键"],
        ["message_event", "unique(worker_id, conversation_id, dedupe_key)。"],
        ["message_batch", "同一conversation_id最多一个active_batch。"],
        ["reply_action", "reply_action_id全局唯一；同一batch同一generation只能有一个current action。"],
        ["send_receipt", "unique(reply_action_id)，sent_ack只能成功写入一次。"],
        ["follow_up_task", "unique(lead_id, rule_id, recall_round)。"],
        ["handoff_event", "unique(conversation_id, handoff_reason_group, active_period)。"],
    ], [42 * mm, 116 * mm], small))
    sub(story, "3.4 执行台展示与验收", h2)
    bullets(story, [
        "展示当前Worker状态、任务类型、任务ID、客户/线索短码、步骤时间线、微信/服务端连接、图片缩略图、AI候选回复、Guard结果、风控原因、飞书通知结果、错误日志。",
        "提供启动、暂停、继续、停止、手动接管/禁用AI、重试、跳过按钮。",
        "验收要求：看得见、停得住、查得到原因、不会重复发送、三个任务类型共用同一UI锁。",
    ], body)

    section(story, "4. 模块3：线索与销售分配", h1)
    bullets(story, [
        "目标：把抖音小风车手机号线索变成某个销售微信号要执行的add_friend任务。",
        "第一期线索接入方式不锁死Excel、CSV或API，统一抽象为线索接入适配器；后续可接小风车/API。",
        "手机号默认脱敏展示；手机号标准化后作为核心去重键。",
        "同手机号一旦标记rejected，后续再次导入也不自动处理。",
        "销售每日加好友上限需要配置，默认值待定。",
        "分配策略待定，先预留手动分配和轮询分配。",
    ], body)
    story.append(table([
        ["状态/规则", "说明"],
        ["Lead状态", "new、assigned、add_friend_pending、add_friend_sent、friend_added、ai_chatting、watching、handoff_required、human_active、rejected、closed。"],
        ["任务生成条件", "手机号有效、销售启用、Worker绑定、未超上限、不在黑名单、不存在未完成add_friend任务。"],
        ["重复手机号", "同手机号未关闭线索不重复新建有效线索，追加来源记录。"],
        ["销售Worker", "一对一绑定，允许换绑；新任务派给新Worker，运行中任务需人工确认。"],
    ], [34 * mm, 124 * mm], small))
    sub(story, "4.1 页面与验收", h2)
    bullets(story, [
        "线索列表展示手机号后四位、来源、销售、状态、最近联系时间、是否转人工、是否拒绝、召回次数、创建时间。",
        "销售列表展示销售姓名、微信号、绑定Worker、飞书用户、启用状态、今日加好友数、今日AI回复数。",
        "任务列表展示任务ID、手机号后四位、销售、Worker、状态、失败原因、重试次数、创建/完成时间。",
        "验收：可导入/接入线索、去重、手动分配、绑定Worker、生成add_friend任务、失败原因可见、备注短码唯一。"],
        body)

    section(story, "5. 模块4：加好友 add_friend", h1)
    bullets(story, [
        "目标：通过商家侧电脑微信桌面客户端，按手机号提交好友申请，并建立初始绑定标识。",
        "不负责线索分配、AI聊天、自动召回、意向判断、飞书接管通知和转人工备注。",
        "好友申请语最终文案待定，作为配置项；默认可支持销售姓名、门店名、线索来源变量。",
        "初始备注用于线索与微信会话初始绑定，命名规则待定；转人工阶段不修改备注。",
        "已是好友时立即尝试绑定会话，不重复发送好友申请。",
        "加好友失败后允许人工在控制面点击重试。",
    ], body)
    story.append(table([
        ["状态/异常", "处理"],
        ["执行状态", "pending、running、searching、result_detected、applying、remarking、succeeded、failed、skipped、paused。"],
        ["可重试失败", "wechat_not_login、wechat_window_not_found、ui_element_not_found、network_error、worker_interrupted、unknown_error。"],
        ["不建议自动重试", "phone_invalid、phone_not_found、already_friend、customer_privacy_blocked、wechat_rate_limit、operation_too_frequent、account_restricted、blacklist_hit、daily_limit_reached。"],
        ["微信风险提示", "操作频繁、环境异常、添加受限等出现后暂停add_friend并上报，不自动连续重试。"],
        ["幂等", "同一task_id多次上报成功只记录一次；同手机号不生成多个未完成加好友任务。"],
    ], [38 * mm, 120 * mm], small))

    section(story, "6. 模块5：会话绑定与监听", h1)
    bullets(story, [
        "目标：知道微信里这条消息是谁发的、属于哪条线索、当前AI能不能回。",
        "第一期不读取微信数据库、不破解协议、不使用非公开微信接口、不依赖客户昵称唯一性。",
        "绑定优先通过初始备注/短码；已是好友立即尝试绑定；绑定失败不自动回复。",
        "监听客户文字、图片、系统提示和我方消息；图片message type为3作为已知线索，实际实现保留兜底。",
        "重复消息通过dedupe_key去重；同一dedupe_key只处理一次。",
    ], body)
    sub(story, "6.1 销售人工回复检测", h2)
    codeblock(story, "Worker发送AI回复前登记reply_action_id、reply_text_hash、send_started_at、send_finished_at。桌面端同步出我方消息时，如果内容和时间匹配AI发送登记表，则标记ai_worker；否则视为human_sales，状态转human_active，ai_enabled=false。", code)
    sub(story, "6.2 重启恢复", h2)
    bullets(story, [
        "Worker启动后读取本地快照，再向服务端确认真实状态，以服务端为准。",
        "未完成pending_action必须检查是否已发送、是否过期、是否仍允许AI、是否已有销售人工消息、是否有客户新消息覆盖旧上下文。",
        "不满足条件时跳过、重新生成或转人工；禁止盲发旧回复。"],
        body)
    sub(story, "6.3 消息批处理与旧回复作废规则", h2)
    story.append(p("本规则由服务端会话调度器执行，不由大模型判断。模型只接收服务端整理后的最新message_batch和evidence_pack，用于生成候选回复。", body))
    story.append(table([
        ["规则", "说明"],
        ["单会话唯一active_batch", "同一conversation_id同一时间只能有一个active_batch。"],
        ["会话内合并", "同一客户短时间多条消息合并为一个message_batch；合并窗口和最大等待时间配置化。"],
        ["会话间排队", "不同客户的batch按首条消息到达时间排队；同会话新消息更新当前batch，不改变初始排队位置。"],
        ["生成中收到新消息", "若A1已进入AI生成但reply_action尚未sent，A2到来后并入当前batch，旧reply_action标记superseded/cancelled，并基于A1+A2重新生成。"],
        ["Worker可执行动作", "Worker只能执行最新有效reply_action_id；superseded、cancelled、expired、sent状态均不得发送。"],
        ["sending状态", "若旧reply_action已进入sending或Worker已持有UI锁开始输入/发送，不强行取消；等待sent_ack或failed_ack，A2进入下一轮batch。"],
        ["已发送后新消息", "旧reply_action已sent后，新消息创建下一轮batch，不撤回已发送消息。"],
        ["模型职责边界", "模型不判断是否取消旧回复、不判断batch合并、不决定发送顺序，只生成候选回复。"],
    ], [38 * mm, 120 * mm], small))
    codeblock(story, "示例：A1、B1、C1、A2依次到达。若A1尚未发送，则A_batch=[A1,A2]，B_batch=[B1]，C_batch=[C1]；发送顺序按首条消息到达时间建议为A、B、C。", code)
    sub(story, "6.4 服务端事务与发送确认", h2)
    story.append(table([
        ["步骤", "事务/状态要求"],
        ["接收消息", "先写message_event唯一键；重复dedupe_key直接返回已处理结果，不再创建batch。"],
        ["合并batch", "在conversation_id维度加行级锁或等效互斥；更新active_batch版本号batch_version。"],
        ["生成回复", "生成完成时检查batch_version未变化、会话仍ai_enabled、未human_active；否则标记superseded。"],
        ["下发Worker", "只下发status=current且未过期的reply_action；同时写入dispatch记录。"],
        ["Worker发送前", "再次向服务端claim reply_action；服务端原子更新queued->sending，失败则Worker不得发送。"],
        ["Worker发送后", "写入sent_ack；sent_ack成功后reply_action变sent。Worker本地失败但服务端未知时，恢复时必须先查sent_ack。"],
        ["恢复扫描", "sending超时进入unknown_send_result，不自动补发；需要Worker截图/人工确认或重新生成下一轮回复。"],
    ], [35 * mm, 123 * mm], small))
    sub(story, "6.5 验收", h2)
    bullets(story, [
        "可绑定微信会话；未绑定会话不自动回复。",
        "客户文字和图片可识别并上传。",
        "Worker发送的AI消息不会误判为销售人工回复。",
        "销售手机端人工回复后AI停止。",
        "同一客户短时间多条消息可合并为一个message_batch。",
        "生成中但未发送的旧reply_action在新消息到来后会被superseded/cancelled，不会被Worker发送。",
        "reply_action从queued到sending再到sent_ack必须有服务端原子状态流转。",
        "重启/断网恢复后不重复发送同一reply_action_id。"],
        body)

    section(story, "7. 模块6：AI对话模块", h1)
    bullets(story, [
        "目标：根据客户消息、上下文、知识库、车源事实和规则生成候选回复或接管建议。",
        "第一版模型使用DeepSeek；模型调用、RAG、车源检索、Guard均在服务端。",
        "OmniAuto作为主对话控制引擎，负责上下文整理、RAG调用、evidence pack消化、候选回复生成、风格控制、轮次控制。",
        "Dify/FastGPT Adapter第一期只预留不实现，不接管主状态。",
        "OmniAuto现有RAG能力需要先做代码评估；知识库资料由项目方整理。",
        "模型失败直接转人工，不使用兜底话术继续自动回复。",
        "AI只输出候选回复和动作建议，不拥有最终发送权。"],
        body)
    story.append(table([
        ["主题", "设计"],
        ["RAG方式", "RAG + 语义检索 + 关键词加权检索。语义检索理解意思，关键词检索抓住泡水、火烧、事故、贷款、定金、底价等关键风险词。"],
        ["Evidence Pack", "包含conversation_context、customer_message、retrieved_knowledge、matched_cars、image_intent、risk_flags、allowed_fields。"],
        ["AI可见车源字段", "品牌、车系、车型、年份、里程、城市、颜色、燃料、配置摘要、对外可说价格、车辆图片。"],
        ["AI不可见字段", "采购价、销售底价、经理价、车主姓名、手机号、身份证、银行卡、内部备注。"],
        ["动作输出", "send_reply、handoff、no_action、pause、retry_later，均带reply_action_id和expire_at。"],
        ["调度边界", "message_batch合并、旧pending_action作废、发送顺序和幂等判断由服务端会话调度器负责，不交给模型判断。"],
        ["20条上限", "达到上限默认watching，AI停止，不触发飞书；风险/高意向转人工；拒绝转rejected。"],
    ], [35 * mm, 123 * mm], small))
    sub(story, "7.1 Guard检查", h2)
    bullets(story, [
        "检查是否承诺无事故、无泡水、无火烧，是否承诺底价、最低价、贷款包过、定金可退，是否涉及合同、赔偿、投诉、法务，是否暴露系统规则或敏感字段。",
        "Guard结果为pass、rewrite、handoff、block；不通过时不发送原文。"],
        body)
    story.append(table([
        ["Guard层", "说明"],
        ["字段隔离", "服务端构造evidence pack时先按白名单过滤，敏感字段不进入模型上下文。"],
        ["规则检查", "发送前使用规则词表检查底价、包过、绝对承诺、投诉法务等明确风险。"],
        ["模型复核", "对候选回复做二次安全判断，输出pass/rewrite/handoff/block及原因。"],
        ["人工接管", "规则或模型任一层判断handoff/block时，默认停止AI并触发接管。"],
        ["审计记录", "保存召回知识片段、候选回复、Guard结论、改写原因和最终动作。"],
    ], [35 * mm, 123 * mm], small))
    sub(story, "7.2 RAG与知识库验收口径", h2)
    story.append(table([
        ["项目", "验收口径"],
        ["OmniAuto评估", "开发前完成现有RAG代码评估，输出可复用、需重构、需新增清单。"],
        ["知识库标准", "知识条目需有标题、适用场景、正文、禁说内容、更新时间、负责人；过期内容不得进入正式索引。"],
        ["检索方式", "采用语义检索+关键词加权；事故、泡水、火烧、底价、贷款、合同等风险词必须被关键词层召回。"],
        ["低置信处理", "知识不足、检索冲突、车源证据不足、模型不确定时转人工，不编造。"],
        ["优化目标", "RAG命中率、误召回率、转人工率作为灰度期优化指标，不作为未经样本集验证的硬承诺。"],
    ], [35 * mm, 123 * mm], small))

    section(story, "8. 模块7：图片理解与图文回复", h1)
    bullets(story, [
        "图片采集在Worker本地完成，图片理解在服务端完成。",
        "第一版视觉模型使用千问视觉；低置信度全部转人工。",
        "客户多张图片第一期逐张处理。",
        "图片本地保存路径固定可配置；保存周期配置化，默认一年。",
        "云端只保存必要文件和识别结果，不做长期图片库。",
        "视觉模型只负责看懂图片，不直接生成最终客服话术。"],
        body)
    codeblock(story, "客户图片 -> Worker识别type=3 -> 点开另存 -> 上传服务端 -> 千问视觉 -> ImageIntent -> 车源索引 -> evidence pack -> OmniAuto候选回复 -> Guard -> send_reply或handoff", code)
    story.append(table([
        ["ImageIntent字段", "说明"],
        ["image_type", "car_photo、car_listing_screenshot、price_screenshot、finance_screenshot、inspection_report、chat_screenshot、unrelated、unknown。"],
        ["detected_vehicle", "品牌、车系、车型、年份、颜色等。"],
        ["detected_price", "识别到的图片价格与置信度。"],
        ["customer_intent", "find_similar_car、ask_price、ask_condition、ask_finance、compare_car、unknown。"],
        ["risk_flags", "price、condition、finance、contract等。"],
    ], [38 * mm, 120 * mm], small))
    bullets(story, [
        "图片保存失败、上传失败、视觉失败、低置信度均不强行图文回复，按规则转人工。",
        "图片也必须遵守message_dedupe_key、image_dedupe_key、reply_action_id、sent_ack，避免重复识别和重复回复。"],
        body)

    section(story, "9. 模块8：大风车与车源索引", h1)
    story.append(p("该模块整体待确认。当前阶段应向大风车提供API需求清单，对照其开放接口确认是否满足，不足部分再列沟通清单。第一期不应写死接口细节。", body))
    story.append(table([
        ["API需求", "说明"],
        ["店铺信息查询", "根据shopCode查询店铺信息，确认门店权限和基础信息。"],
        ["车辆ID列表", "根据shopCode和operationPhase查询车辆ID，需operationPhase枚举说明和可售状态定义。"],
        ["车辆详情", "按carId查询品牌、车系、车型、年份、里程、颜色、配置、状态、对外展示价格等。"],
        ["车辆图片", "按carId查询图片URL、图片名称、图片类型、排序、大图/缩略图。"],
        ["增量同步", "确认是否支持按更新时间查询变更车辆、分页、车辆状态变更同步或Webhook。"],
        ["鉴权与限制", "确认appKey、appSecret、appId、shopCode、operator、IP白名单、频率限制、错误码、测试环境。"],
    ], [36 * mm, 122 * mm], small))
    bullets(story, [
        "大风车作为权威车源系统，不作为图片搜车接口。",
        "服务端同步后分为raw_vehicle原始数据层和vehicle_index AI可见索引层。",
        "AI只读白名单字段；采购价、底价、经理价、车主隐私、内部备注默认隔离。",
        "同步失败保留上次成功索引，不阻塞普通聊天；鉴权失败需告警。"],
        body)
    sub(story, "9.1 Gate 0接口确认", h2)
    story.append(table([
        ["确认项", "未满足时处理"],
        ["鉴权参数与IP白名单", "不能联调大风车；改用本地车源导入/样本索引完成AI链路验收。"],
        ["operationPhase枚举与可售状态", "不能自动判断在售范围；需人工配置可售状态白名单。"],
        ["对外价格字段", "不能让AI回答具体价格；价格问题默认转人工或只回复需销售确认。"],
        ["车辆详情与图片字段", "字段不足则降低图片找车准确性；按可用字段建索引并标注缺失。"],
        ["增量同步能力", "无增量接口时使用定时全量/车辆ID轮询；同步频率和成本需另行确认。"],
        ["频率限制与错误码", "无明确限制时按保守频率调用；错误原因不可识别时进入告警和人工确认。"],
    ], [45 * mm, 113 * mm], small))

    section(story, "10. 模块9：风控策略中心", h1)
    bullets(story, [
        "风控属于云端业务控制面的核心子模块，但作为独立业务模块详细设计。",
        "风控策略由服务端控制面配置和判定，Worker执行服务端返回的动作，并展示命中原因。",
        "第一期不承诺规避微信平台风控，不做复杂反检测和机器学习风控模型。"],
        body)
    story.append(table([
        ["风控项", "口径"],
        ["自动回复总开关", "可按全局、销售、Worker、会话控制；关闭后不自动回复和召回。"],
        ["人工接管模式", "handoff_required或human_active时AI必须停止。"],
        ["静默时段", "客户主动发消息也完全不自动回复；召回必须延期或跳过。"],
        ["每日上限", "AI回复、加好友、召回上限均配置化，默认待定。"],
        ["黑名单", "第一期支持，用于拒绝、投诉、无效、不再跟进客户。"],
        ["白名单", "预留或仅支持测试手机号，不能绕过高风险接管。"],
        ["关键词拦截", "投诉、报警、律师、退款、赔偿、诈骗、别联系等。"],
        ["人工接管关键词", "底价、事故、泡水、贷款、定金、合同、地址、现在定等，销售和项目方确认，最终项目方确认。"],
        ["随机发送延迟", "配置化，默认待定；仅体验优化，不承诺规避微信风控。"],
        ["风险提示检测", "操作频繁、环境异常、添加受限等出现后暂停任务并上报。"],
        ["单会话突发限频", "配置化，默认待定。"],
        ["风险暂停恢复", "支持人工解除或到期自动解除，默认人工确认更稳。"],
    ], [36 * mm, 122 * mm], small))
    sub(story, "10.1 执行顺序", h2)
    codeblock(story, "总开关 -> 会话状态 -> 黑名单 -> 白名单 -> 静默时段 -> 每日上限 -> 单会话限频 -> 关键词拦截 -> 人工接管关键词 -> 模型/图片/车源异常 -> Guard发送前检查", code)

    section(story, "11. 模块10：人工接管与飞书通知", h1)
    bullets(story, [
        "目标：AI停手，销售接上。",
        "接管状态在云端控制面；飞书通知由服务端触发；Worker停止该会话自动回复并展示状态。",
        "第一期使用飞书机器人定向通知销售个人，不做短信通知。",
        "接管后客户继续发消息不再次提醒销售；销售长时间不接管不做二次自动提醒。",
        "第一期不做飞书重发按钮、不做“我已接管”按钮、不单独增加飞书通知角色和权限。"],
        body)
    story.append(table([
        ["触发来源", "说明"],
        ["风控/关键词", "高风险、高意向、投诉、金融、合同、底价等。"],
        ["模型失败", "DeepSeek超时或失败、RAG失败、图片视觉失败、低置信度、车源失败无法安全回复。"],
        ["销售主动回复", "检测到销售手机端人工消息。"],
        ["手动操作", "控制面或Worker执行台点击停止AI/手动接管。"],
    ], [38 * mm, 120 * mm], small))
    bullets(story, [
        "进入handoff_required时必须ai_enabled=false。",
        "飞书通知包含客户标识、线索短码、手机号后四位、销售、触发原因、最近消息、建议动作、时间。",
        "飞书通知失败时AI仍停止，控制面和Worker执行台展示错误日志，由项目方人工查看处理。",
        "同一handoff_event_id只触发一次飞书通知，避免重复提醒销售。"],
        body)
    sub(story, "11.1 飞书通知轻量实现", h2)
    story.append(table([
        ["机制", "要求"],
        ["触发", "服务端进入handoff_required后调用飞书机器人发送一次通知。"],
        ["记录", "在HandoffEvent中记录notify_status=sent/failed、请求时间、返回码、错误摘要。"],
        ["失败处理", "不做自动重发和手动重发；失败时保留错误日志，项目方自行查看并人工处理。"],
        ["幂等", "同一handoff_event_id只允许触发一次通知。"],
        ["降级", "飞书失败不恢复AI自动回复；会话继续保持接管状态。"],
    ], [35 * mm, 123 * mm], small))

    section(story, "12. 模块11：自动召回 follow_up", h1)
    bullets(story, [
        "目标：对已添加微信、处于watching、长期未互动且未拒绝的客户做低频再触达。",
        "云端判断资格并生成follow_up任务，Worker只发送固定召回文案和上报结果。",
        "第一期只做一种召回规则；周期默认待定，可配置为7天或14天。",
        "召回固定文案待定，可配置；第一期不让模型自由生成召回话术。",
        "每个客户最多召回1次；每日召回上限待定；扫描频率每天一次。",
        "watching第一期支持人工标记，同时支持简单规则自动标记。"],
        body)
    story.append(table([
        ["规则", "说明"],
        ["适用客户", "已加好友、会话已绑定、status=watching、未拒绝、未转人工、未关闭、未黑名单、最近N天无客户/销售消息。"],
        ["自动标记watching", "客户表达再看看、考虑一下、晚点说等观望，或AI达到最大回复轮次且未拒绝、未接管。"],
        ["排除条件", "rejected、handoff_required、human_active、closed、黑名单、近期客户/销售已联系、达到召回上限、风控暂停、静默时段。"],
        ["发送", "follow_up获取Local WeChat UI Lock后发送固定文案。"],
        ["防重复", "同一客户同一规则周期只发送一次，同一follow_up_task_id只发送一次，重启后已sent不再发送。"],
    ], [38 * mm, 120 * mm], small))

    section(story, "13. 模块12：测试、验收与部署", h1)
    bullets(story, [
        "核心验收原则：系统能正常回复，或在不能安全回复时触发人工接管。",
        "性能目标不作为未经压测的硬承诺，最终以测试环境、账号状态、网络质量、模型服务和真实样本实测为准。",
        "测试环境包含云端控制面、数据库、AI服务、RAG/知识库、车源索引、风控配置、飞书机器人、商家侧Windows电脑、微信桌面端、Worker执行台、销售手机微信和飞书。"],
        body)
    story.append(table([
        ["测试阶段", "内容"],
        ["P1基础链路", "线索接入、销售分配、Worker绑定、add_friend、初始备注绑定。"],
        ["P2文字回复", "客户文字、RAG、DeepSeek、Guard、Worker发送、审计。"],
        ["P3图片回复", "图片另存、上传、千问视觉、ImageIntent、车源索引、图文回复或转人工。"],
        ["P4风控接管", "总开关、静默、上限、黑名单、关键词、模型失败、飞书通知、销售回复后AI停止。"],
        ["P5自动召回", "watching、N天未联系、每天扫描、固定文案、上限、跳过原因、防重复。"],
        ["P6异常恢复", "Worker断网/重启、服务端不可用、AI超时、重复消息、pending_action恢复、不重复发送。"],
    ], [35 * mm, 123 * mm], small))
    sub(story, "13.1 S1阻塞缺陷", h2)
    bullets(story, [
        "无法加好友、无法监听消息、无法发送回复、AI无法停止、重复发送同一回复、转人工后仍自动回复、敏感字段泄露。"],
        body)
    sub(story, "13.2 缺陷分级与外部依赖验收", h2)
    story.append(table([
        ["级别", "定义", "处理"],
        ["S1阻塞", "主链路不可用、重复发送、AI停不住、敏感字段泄露、错误接管后继续回复。", "必须修复后验收。"],
        ["S2严重", "部分场景失败但有人工降级，例如图片低置信过多、大风车同步失败但保留旧索引。", "需给出修复计划或降级方案。"],
        ["S3一般", "体验问题、配置默认值调整、页面展示不完整但不影响主链路。", "可进入试运行问题清单。"],
        ["外部依赖", "微信版本变化、账号受限、模型服务故障、大风车未开放字段、飞书配置不可用。", "按降级方案和责任边界处理，不直接归为内部开发缺陷。"],
    ], [25 * mm, 58 * mm, 75 * mm], small, font_size=6.8))
    sub(story, "13.3 必测用例补充", h2)
    bullets(story, [
        "A、B、C同时来消息，A追加第二条：验证同会话合并、跨会话排队、旧reply_action作废。",
        "AI生成中断网、Worker重启、服务端重启：验证不会重复发送。",
        "Worker进入sending后异常退出：状态进入unknown_send_result，不自动补发。",
        "销售手机端回复后桌面端同步：验证AI停止且不再召回。",
        "微信出现操作频繁/添加受限提示：验证Worker暂停、截图、告警、加好友不继续冲。",
        "飞书发送失败：验证AI仍停止，HandoffEvent记录失败状态和错误日志，控制面/Worker执行台可见。",
        "大风车鉴权失败/字段缺失/无可售车：验证按Gate 0降级，不编造车源。",
    ], body)

    section(story, "14. 支撑模块", h1)
    story.append(table([
        ["支撑模块", "第一期口径"],
        ["日志审计与数据留痕", "记录任务、消息、RAG召回、候选回复、Guard、风控、飞书通知、Worker错误、人工操作，敏感字段脱敏。"],
        ["配置中心与运维监控", "集中管理模型、风控、召回、销售/Worker绑定、图片保留周期、车源同步等配置，展示Worker在线与同步状态。"],
        ["数据安全与权限边界", "第一期轻量权限；模型Key、大风车密钥、飞书配置不下发Worker；AI只读白名单字段。"],
        ["Worker兼容性管理", "记录Windows、微信、Worker版本；每次微信升级前跑核心回归；支持暂停Worker和人工降级。"],
        ["异常恢复任务", "定时扫描stale running、unknown_send_result、vehicle sync failed，生成待办；飞书失败仅记录错误日志。"],
    ], [42 * mm, 116 * mm], small))

    section(story, "15. 总待确认清单", h1)
    bullets(story, [
        "Gate 0阻塞项：大风车API接口、字段、operationPhase枚举、可售状态、对外价格字段、鉴权/IP白名单，待确认。",
        "Gate 0阻塞项：销售手机端人工回复同步到桌面端后的可读结构，需真实微信环境实测。",
        "Gate 0阻塞项：OmniAuto现有RAG能力需先做代码评估。",
        "开发可并行项：线索接入方式Excel、CSV、手动录入或API，待确认，先按适配器实现。",
        "开发可并行项：线索分配策略手动、轮询或其他规则，待确认，先保留配置位。",
        "配置待确认项：好友申请语最终文案。",
        "配置待确认项：初始备注命名规则。",
        "配置待确认项：每日加好友、AI回复、召回上限默认值。",
        "配置待确认项：随机发送延迟范围和单会话限频默认规则。",
        "配置待确认项：召回周期默认值、召回固定文案。",
        "联调待确认项：飞书机器人定向个人通知的具体实现方式和错误返回格式。",
    ], body)

    def header_footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("STSong-Light", 8)
        canvas.setFillColor(colors.HexColor("#6B7280"))
        canvas.drawString(16 * mm, 287 * mm, f"{PROJECT_NAME} 技术方案手册 {DOC_VERSION}")
        canvas.drawRightString(194 * mm, 10 * mm, f"第 {canvas.getPageNumber()} 页")
        canvas.restoreState()

    doc.build(story, onFirstPage=header_footer, onLaterPages=header_footer)


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    generate_pdf()
    print(PDF_PATH)


if __name__ == "__main__":
    main()
