from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

from build_delivery_artifacts import PROJECT_NAME, ROOT, UNIT_PRICE, bullet_list, make_table, p


DOC_VERSION = "v2.0"
DOC_DATE = "2026-05-24"
PDF_PATH = ROOT / f"{PROJECT_NAME}_技术方案手册_{DOC_VERSION}.pdf"


def make_styles():
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "CNBodyV20",
        parent=styles["BodyText"],
        fontName="STSong-Light",
        fontSize=9,
        leading=12.8,
        spaceAfter=4,
        textColor=colors.HexColor("#1F2937"),
    )
    small = ParagraphStyle(
        "CNSmallV20",
        parent=body,
        fontSize=7.6,
        leading=10.2,
        spaceAfter=2,
    )
    title = ParagraphStyle(
        "CNTitleV20",
        parent=styles["Title"],
        fontName="STSong-Light",
        fontSize=22,
        leading=28,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#0B2545"),
        spaceAfter=10,
    )
    subtitle = ParagraphStyle(
        "CNSubtitleV20",
        parent=body,
        fontSize=11,
        leading=16,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#4B5563"),
        spaceAfter=12,
    )
    h1 = ParagraphStyle(
        "CNH1V20",
        parent=styles["Heading1"],
        fontName="STSong-Light",
        fontSize=13.5,
        leading=17,
        textColor=colors.HexColor("#1F4E78"),
        spaceBefore=7,
        spaceAfter=5,
    )
    h2 = ParagraphStyle(
        "CNH2V20",
        parent=styles["Heading2"],
        fontName="STSong-Light",
        fontSize=10.8,
        leading=14,
        textColor=colors.HexColor("#2E74B5"),
        spaceBefore=5,
        spaceAfter=3,
    )
    code = ParagraphStyle(
        "CNCodeV20",
        parent=body,
        fontName="STSong-Light",
        fontSize=7.8,
        leading=10,
        backColor=colors.HexColor("#F4F6F9"),
        borderColor=colors.HexColor("#E5E7EB"),
        borderWidth=0.35,
        borderPadding=5,
        spaceBefore=3,
        spaceAfter=5,
    )
    return body, small, title, subtitle, h1, h2, code


def table(data, widths, style, repeat=1, font_size=7.6):
    return make_table([[p(str(c), style) for c in row] for row in data], widths, repeat=repeat, font_size=font_size)


def add_decision_table(story, rows, small):
    story.append(table([["事项", "已确认口径"]] + rows, [50 * mm, 108 * mm], small, repeat=1))


def generate_pdf() -> None:
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    body, small, title, subtitle, h1, h2, code = make_styles()
    doc = SimpleDocTemplate(
        str(PDF_PATH),
        pagesize=A4,
        rightMargin=17 * mm,
        leftMargin=17 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f"{PROJECT_NAME} 技术方案手册 {DOC_VERSION}",
    )

    story = []
    story.append(Spacer(1, 12 * mm))
    story.append(p(PROJECT_NAME, title))
    story.append(p("技术方案手册（详细设计确认版）", subtitle))
    cover = [
        ["文档版本", DOC_VERSION],
        ["文档日期", DOC_DATE],
        ["预算基线", f"87,000元（58人天 × {UNIT_PRICE}元/人天）"],
        ["交付口径", "第一期正式工程版本；能正常回复、按规则召回或触发人工接管；性能指标以测试环境实测为准"],
        ["核心架构", "云端业务控制面 + 商家侧本地Worker + OmniAuto/DeepSeek + 千问视觉 + 车源索引 + 风控/接管"],
    ]
    story.append(table(cover, [36 * mm, 122 * mm], body, repeat=0, font_size=9))
    story.append(Spacer(1, 6 * mm))
    story.append(p("本文档整理截至当前讨论已确认的模块级详细设计，用于后续研发拆解、验收用例编写和范围控制。未确认事项均以“待定/待确认”标识，不作为硬性交付承诺。", body))
    story.append(PageBreak())

    story.append(p("1. 总体原则", h1))
    story.append(bullet_list([
        "云端负责状态、配置、调度、AI、RAG、车源索引、风控、飞书通知和审计。",
        "商家侧Worker只负责微信桌面端操作、消息采集、图片另存、回复发送和本地执行台展示。",
        "销售人工接管使用微信手机客户端；Worker控制同一销售微信号登录的微信桌面客户端。",
        "转人工不修改微信备注；接管状态以系统内部状态为准，并通过飞书机器人通知销售手机端。",
        "任何断网、重启、超时或恢复场景下，不得重复发送同一条AI回复或召回文案。",
        "达到最大AI回复轮次，默认进入watching；不默认close，不默认转人工。",
    ], body))
    story.append(p("核心状态规则", h2))
    status_rules = [
        ["场景", "目标状态", "动作"],
        ["普通聊天达到20条AI回复上限", "watching", "ai_enabled=false；不触发飞书；可进入召回候选"],
        ["命中高风险/高意向/接管关键词", "handoff_required", "ai_enabled=false；触发飞书通知"],
        ["销售手机端人工回复", "human_active", "ai_enabled=false；停止自动回复和自动召回"],
        ["客户明确拒绝", "rejected", "不自动加好友、不自动回复、不自动召回"],
        ["线索结束且无需跟进", "closed", "人工或规则关闭"],
    ]
    story.append(table(status_rules, [45 * mm, 38 * mm, 75 * mm], small))

    story.append(p("2. 模块1：云端业务控制面", h1))
    add_decision_table(story, [
        ["部署位置", "VPS/云服务器；作为业务状态中心、任务调度中心、配置审计中心。"],
        ["登录与权限", "第一期做轻量后台，由项目方自己控制；不做复杂账号和权限体系。"],
        ["销售与Worker", "一名销售一台Worker，一一绑定；允许换绑；换绑需留痕。"],
        ["线索分配", "分配策略待定；预留手动分配和轮询分配。"],
        ["飞书通知", "只做飞书，不做短信；定向通知销售个人。"],
        ["召回规则", "第一期只做一种规则；周期可配置，例如7天或14天，默认待定。"],
        ["控制目标", "状态准确、任务可追踪、配置可调整；不做复杂CRM。"],
    ], small)

    story.append(p("3. 模块2：Worker任务类型与本地执行台", h1))
    story.append(p("Worker是商家侧Windows电脑上的本地执行器，必须呈现为类似附件视频的本地可视化窗口，而不是不可见后台脚本。技术实现可选桌面GUI、本地WebView等，但交互效果应是独立可见、可启动、可暂停、可复盘的执行台。", body))
    worker_rows = [
        ["任务类型", "职责"],
        ["add_friend", "只负责手机号搜索、发送好友申请、写初始绑定备注、回传申请结果。"],
        ["chat_reply", "只负责已绑定会话监听、文字/图片采集、上传服务端、执行服务端返回动作。"],
        ["follow_up", "只负责领取召回任务、发送固定召回文案、上报结果。"],
        ["Local WeChat UI Lock", "所有微信桌面端UI操作必须共用同一把锁，同一时刻只能有一个任务操作微信窗口。"],
    ]
    story.append(table(worker_rows, [42 * mm, 116 * mm], small))
    story.append(p("已确认设计", h2))
    add_decision_table(story, [
        ["执行台形态", "参照附件视频：微信桌面客户端旁边显示Worker执行台，展示步骤时间线、截图证据、AI结果、Guard结果、风控命中、运行控制。"],
        ["图片路径", "允许固定本地目录，路径规则后续实现时配置。"],
        ["开机自启", "第一期不需要；通过执行台启动按钮操作。"],
        ["任务优先级", "chat_reply > add_friend > follow_up；add_friend优先follow_up。"],
        ["等待AI期间", "chat_reply等待服务端AI结果时不占用UI锁，add_friend可先执行。"],
        ["销售人工回复检测", "通过AI发送登记表识别我方消息；不在登记表中的我方消息视为销售手机端人工回复。"],
        ["重启恢复", "以服务端状态为准；pending_action需重新校验会话状态、过期时间、是否已发送、是否已有新上下文。"],
        ["防重复发送", "message_id/dedupe_key + reply_action_id + sent_ack 三层保证；同一reply_action_id只能发送一次。"],
    ], small)

    story.append(p("4. 模块3：线索与销售分配", h1))
    add_decision_table(story, [
        ["线索来源", "不锁死Excel/CSV/API；抽象为线索接入适配器，后续可接小风车/API。"],
        ["手机号展示", "默认脱敏展示。"],
        ["拒绝客户", "同手机号一旦标记rejected，后续再次导入也不自动处理。"],
        ["销售每日加好友上限", "需要配置；默认值待定。"],
        ["分配规则", "轮询分配是否放入第一期待定；先预留。"],
        ["去重", "手机号标准化后作为第一期核心去重键；重复导入不重复生成有效线索。"],
    ], small)

    story.append(p("5. 模块4：加好友 add_friend", h1))
    add_decision_table(story, [
        ["好友申请语", "最终文案待定，作为配置项。"],
        ["初始备注", "用于线索与微信会话初始绑定；命名规则待定。"],
        ["已是好友", "立即尝试绑定会话，不重复发送好友申请。"],
        ["失败重试", "允许人工在控制面点击重试。"],
        ["每日上限", "配置项，默认值待定。"],
        ["转人工备注", "不做；人工接管不修改微信备注。"],
    ], small)

    story.append(p("6. 模块5：会话绑定与监听", h1))
    conv_rows = [
        ["能力", "设计口径"],
        ["绑定", "优先通过初始备注/短码绑定；已是好友时立即尝试绑定会话；绑定失败不自动回复。"],
        ["消息监听", "识别客户文字、图片、系统提示和我方消息；图片消息type=3作为已知线索，保留兜底。"],
        ["人工回复检测", "桌面端同步出我方消息且不匹配AI发送登记表，则判定human_sales。"],
        ["重启恢复", "读取本地快照并向服务端确认；服务端状态优先；不得盲发旧回复。"],
        ["防重复", "同一dedupe_key只处理一次；同一reply_action_id只发送一次。"],
    ]
    story.append(table(conv_rows, [34 * mm, 124 * mm], small))

    story.append(p("7. 模块6：AI对话模块", h1))
    add_decision_table(story, [
        ["模型", "第一版使用DeepSeek。"],
        ["部署", "大模型、RAG、车源检索、Guard放服务端；Worker不持有模型API Key。"],
        ["RAG", "OmniAuto现有RAG能力需先做代码评估；知识库采用RAG + 语义/关键词加权混合检索。"],
        ["知识库资料", "由项目方整理提供。"],
        ["接管关键词", "销售和项目方共同确认，最终由项目方确认。"],
        ["Dify/FastGPT", "第一期只预留Adapter，不实现，不接管主状态。"],
        ["模型失败", "直接转人工，不使用兜底话术继续自动回复。"],
        ["20条上限", "达到最大AI回复轮次后进入watching，AI停止；若命中风险/高意向则转人工；若拒绝则rejected。"],
    ], small)

    story.append(p("8. 模块7：图片理解与图文回复", h1))
    add_decision_table(story, [
        ["视觉模型", "第一版使用千问视觉。"],
        ["低置信度", "全部转人工。"],
        ["多图处理", "第一期逐张处理。"],
        ["图片保存周期", "配置化，默认一年。"],
        ["云端保存", "只保存必要文件和识别结果，不做长期图片库。"],
        ["职责边界", "Worker负责另存/上传；服务端负责视觉理解、ImageIntent、车源匹配和OmniAuto回复。"],
    ], small)

    story.append(p("9. 模块8：大风车与车源索引", h1))
    story.append(p("本模块当前整体待确认。大风车正在询问所需API接口，需先给对方API需求清单，再根据对方反馈确认实现范围。", body))
    api_rows = [
        ["API类别", "需求"],
        ["店铺信息查询", "根据shopCode查询店铺信息和权限范围。"],
        ["车辆ID列表", "按shopCode + operationPhase获取车辆ID；需operationPhase枚举和可售状态说明。"],
        ["车辆详情", "按carId获取品牌、车系、车型、年份、里程、颜色、配置、状态、对外价格等。"],
        ["车辆图片", "按carId获取图片URL、名称、类型、排序、大图/缩略图。"],
        ["增量能力", "确认是否支持按更新时间增量拉取、分页、车辆状态变更同步或Webhook。"],
        ["鉴权限制", "确认appKey/appSecret/appId/shopCode/operator、IP白名单、频率限制、错误码和测试环境。"],
    ]
    story.append(table(api_rows, [36 * mm, 122 * mm], small))
    story.append(p("价格字段需重点确认：哪个字段是对外可说价格；哪些字段为采购价、底价、经理价、车主隐私，不能进入AI可见索引。", body))

    story.append(p("10. 模块9：风控策略中心", h1))
    risk_rows = [
        ["风控项", "确认口径"],
        ["静默时段", "客户主动发消息也完全不自动回复。"],
        ["每日上限", "AI回复上限、加好友上限、召回上限均配置化，默认值待定。"],
        ["黑名单", "第一期支持；用于拒绝客户、投诉客户、无效号码等不再自动处理。"],
        ["白名单", "预留，或仅支持测试手机号；白名单不能绕过高风险接管。"],
        ["接管关键词", "销售和项目方共同确认，最终项目方确认。"],
        ["随机延迟", "配置化，默认范围待定；不承诺规避微信平台风控。"],
        ["单会话突发限频", "配置化，默认规则待定。"],
        ["风险暂停恢复", "支持人工解除或到期自动解除；默认人工确认更稳。"],
    ]
    story.append(table(risk_rows, [40 * mm, 118 * mm], small))

    story.append(p("11. 模块10：人工接管与飞书通知", h1))
    add_decision_table(story, [
        ["通知方式", "第一期使用飞书机器人，定向通知销售个人；不做短信。"],
        ["我已接管按钮", "预留，不作为第一期必做。"],
        ["接管后客户继续发消息", "不再次提醒销售。"],
        ["销售长时间不接管", "不做二次自动提醒。"],
        ["手动重发通知", "建议保留轻量保护：仅管理员/项目方操作，限制次数，操作留痕，按钮确认。"],
        ["通知失败", "AI仍停止；控制面和Worker执行台展示告警；允许人工重发。"],
    ], small)

    story.append(p("12. 模块11：自动召回 follow_up", h1))
    add_decision_table(story, [
        ["召回周期", "默认值待定，可配置，例如7天或14天。"],
        ["固定文案", "待定，可配置；第一期不让模型自由生成召回话术。"],
        ["每客户最多召回", "1次。"],
        ["每日召回上限", "默认值待定，可配置。"],
        ["扫描频率", "每天一次。"],
        ["watching状态", "第一期支持人工标记，同时支持简单规则自动标记。"],
        ["自动标记watching", "客户表达观望，或AI达到最大回复轮次且未拒绝、未转人工。"],
        ["排除条件", "rejected、human_active、handoff_required、closed、黑名单、近期客户/销售已联系均不召回。"],
    ], small)

    story.append(p("13. 模块12：测试、验收与部署", h1))
    acceptance = [
        ["类别", "验收重点"],
        ["核心闭环", "线索导入/接入、销售分配、add_friend、会话绑定、文字回复、图片处理、接管、召回。"],
        ["不重复发送", "Worker断网、重启、服务端超时、pending_action恢复后，同一reply_action_id或follow_up_task_id不得重复发送。"],
        ["人工接管", "命中接管后ai_enabled=false；飞书机器人通知销售个人；销售手机端回复后human_active。"],
        ["风控", "总开关、静默、上限、黑名单、关键词、随机延迟、风险提示、单会话限频可配置或可验证。"],
        ["Worker执行台", "本地可视化窗口，显示任务类型、步骤、截图证据、AI/Guard/风控结果、错误原因，支持启动/暂停/停止。"],
        ["缺陷门槛", "S1缺陷为0；S2修复或双方确认规避；性能指标以测试环境实测为准。"],
    ]
    story.append(table(acceptance, [34 * mm, 124 * mm], small))

    story.append(p("14. 支撑模块", h1))
    support = [
        ["支撑模块", "第一期口径"],
        ["日志审计与数据留痕", "记录任务、消息、AI召回、Guard、风控、飞书通知、Worker错误、人工操作；敏感字段脱敏。"],
        ["配置中心与运维监控", "集中管理模型、风控、召回、销售/Worker绑定、图片保留周期、车源同步等配置；展示Worker在线和同步状态。"],
        ["数据安全与权限边界", "第一期轻量权限；模型Key、大风车密钥、飞书配置不下发Worker；AI只读白名单字段。"],
    ]
    story.append(table(support, [42 * mm, 116 * mm], small))

    story.append(p("15. 待确认清单", h1))
    story.append(bullet_list([
        "线索接入方式：Excel、CSV、手动录入或API，待确认。",
        "线索分配策略：手动、轮询或其他规则，待确认。",
        "好友申请语最终文案，待确认。",
        "初始备注命名规则，待确认。",
        "每日加好友、AI回复、召回上限默认值，待确认。",
        "随机发送延迟范围和单会话限频默认规则，待确认。",
        "大风车API接口、字段、operationPhase枚举、可售状态和价格字段，待确认。",
        "召回周期默认值、召回固定文案，待确认。",
        "飞书机器人定向个人通知的具体实现方式和配置权限，待联调确认。",
        "销售手机端人工回复同步到桌面端后的可读结构，需用真实微信环境实测。",
    ], body))

    def header_footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("STSong-Light", 8)
        canvas.setFillColor(colors.HexColor("#6B7280"))
        canvas.drawString(17 * mm, 287 * mm, f"{PROJECT_NAME} 技术方案手册 {DOC_VERSION}")
        canvas.drawRightString(193 * mm, 10 * mm, f"第 {canvas.getPageNumber()} 页")
        canvas.restoreState()

    doc.build(story, onFirstPage=header_footer, onLaterPages=header_footer)


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    generate_pdf()
    print(PDF_PATH)


if __name__ == "__main__":
    main()
