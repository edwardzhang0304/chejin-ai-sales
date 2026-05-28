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

from build_delivery_artifacts import PROJECT_NAME, ROOT, bullet_list, make_table, p


VERSION = "v2.5"
DOC_DATE = "2026-05-27"
PDF_PATH = ROOT / f"{PROJECT_NAME}_验收标准_{VERSION}_正式工程版.pdf"


def make_styles():
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "CNBodyAcceptance",
        parent=styles["BodyText"],
        fontName="STSong-Light",
        fontSize=8.2,
        leading=11.3,
        spaceAfter=3,
        textColor=colors.HexColor("#1F2937"),
    )
    small = ParagraphStyle(
        "CNSmallAcceptance",
        parent=body,
        fontSize=6.5,
        leading=8.2,
        spaceAfter=1.6,
    )
    title = ParagraphStyle(
        "CNTitleAcceptance",
        parent=styles["Title"],
        fontName="STSong-Light",
        fontSize=20,
        leading=26,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#0B2545"),
        spaceAfter=8,
    )
    subtitle = ParagraphStyle(
        "CNSubtitleAcceptance",
        parent=body,
        fontSize=10,
        leading=14,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#4B5563"),
        spaceAfter=8,
    )
    h1 = ParagraphStyle(
        "CNH1Acceptance",
        parent=styles["Heading1"],
        fontName="STSong-Light",
        fontSize=12.2,
        leading=15,
        textColor=colors.HexColor("#1F4E78"),
        spaceBefore=7,
        spaceAfter=4,
    )
    h2 = ParagraphStyle(
        "CNH2Acceptance",
        parent=styles["Heading2"],
        fontName="STSong-Light",
        fontSize=9.6,
        leading=12.2,
        textColor=colors.HexColor("#2E74B5"),
        spaceBefore=4,
        spaceAfter=2,
    )
    code = ParagraphStyle(
        "CNCodeAcceptance",
        parent=body,
        fontName="STSong-Light",
        fontSize=7.1,
        leading=9.2,
        backColor=colors.HexColor("#F4F6F9"),
        borderColor=colors.HexColor("#E5E7EB"),
        borderWidth=0.3,
        borderPadding=4,
        spaceBefore=2,
        spaceAfter=4,
    )
    return body, small, title, subtitle, h1, h2, code


def table(data, widths, style, repeat=1, font_size=6.6):
    return make_table([[p(str(c), style) for c in row] for row in data], widths, repeat=repeat, font_size=font_size)


def add_section(story, text, h1):
    story.append(p(text, h1))


def add_sub(story, text, h2):
    story.append(p(text, h2))


def generate_pdf() -> None:
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    body, small, title, subtitle, h1, h2, code = make_styles()
    doc = SimpleDocTemplate(
        str(PDF_PATH),
        pagesize=A4,
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=14 * mm,
        bottomMargin=13 * mm,
        title=f"{PROJECT_NAME} 验收标准 {VERSION}",
    )

    story = []
    story.append(Spacer(1, 8 * mm))
    story.append(p(PROJECT_NAME, title))
    story.append(p("验收标准", subtitle))
    story.append(table([
        ["文档版本", VERSION],
        ["文档日期", DOC_DATE],
        ["适用范围", "第一期正式工程版本"],
        ["不包含", "二期SaaS化、多商户、复杂权限、计费体系、高可用集群和长期运维"],
    ], [32 * mm, 138 * mm], body, repeat=0, font_size=8.5))
    story.append(PageBreak())

    add_section(story, "1. 验收总原则", h1)
    story.append(p("本期系统的核心验收标准为：", body))
    story.append(p("系统能够根据客户消息正常自动回复；或在无法安全回复、识别失败、命中风险规则、模型失败、图片低置信度、车源证据不足时，正确停止AI并触发人工接管通知。", code))
    story.append(p("验收不以“完全像真人”“百分百识别所有客户意图”“任何微信环境永久稳定”为标准。验收以约定测试环境、真实样本、可复现操作、系统日志和验收用例结果为准。性能指标作为优化目标，不作为未经压测的硬性验收承诺。", body))

    add_section(story, "2. 验收前提", h1)
    story.append(table([
        ["类型", "前提条件", "未满足时处理"],
        ["测试电脑", "提供商家侧Windows测试电脑，安装指定版本微信桌面客户端和Worker。", "无法验收Worker、加好友、监听、图片另存和发送。"],
        ["微信账号", "提供测试销售个人微信，账号可正常登录、加好友、收发消息。", "账号受限、风控或登录异常不计为系统功能缺陷。"],
        ["销售手机端", "提供销售微信手机客户端，用于人工回复和接管验证。", "无法验收销售人工回复后AI停止。"],
        ["线索样本", "提供有效、无效、重复、拒绝客户等手机号样本。", "相关用例顺延。"],
        ["知识库资料", "提供二手车售前知识库、禁说规则、销售话术边界。", "RAG和Guard验收只验证框架，不验证最终业务质量。"],
        ["图片样本", "提供车图、截图、价格图、无关图、低清图等样本。", "图片链路只能用模拟样本验收。"],
        ["大风车条件", "提供appKey、appSecret、appId、shopCode、operator、IP白名单和接口字段说明。", "未满足时按本地车源导入/样本索引降级验收。"],
        ["模型与飞书", "提供DeepSeek、千问视觉、飞书机器人配置。", "对应链路无法完整验收或只验收错误日志。"],
    ], [25 * mm, 88 * mm, 57 * mm], small))

    add_section(story, "3. 缺陷等级", h1)
    story.append(table([
        ["等级", "定义", "验收处理"],
        ["S1阻塞", "主链路不可用或存在严重业务风险：无法加好友、无法监听、无法发送、AI停不住、重复发送、接管后仍自动回复、敏感字段泄露。", "必须修复后验收。"],
        ["S2严重", "核心功能部分失败但有人工降级，例如图片低置信过多、大风车同步失败但保留旧索引、异常未展示清楚。", "需修复或双方确认降级方案后验收。"],
        ["S3一般", "不影响主流程的体验、文案、展示、配置默认值问题。", "可进入试运行问题清单，不阻塞验收。"],
        ["S4建议", "优化建议或新增需求。", "不计入本期缺陷，进入后续变更。"],
        ["外部依赖", "微信版本变化、账号受限、模型服务故障、大风车未开放字段、飞书配置不可用、网络故障。", "按责任边界和降级方案处理，不直接归为系统开发缺陷。"],
    ], [22 * mm, 96 * mm, 52 * mm], small))

    add_section(story, "4. 核心验收用例", h1)
    cases = [
        ["线索与销售", "A-01~A-05", "导入、去重、拒绝客户不自动处理、销售Worker绑定、每日加好友上限。"],
        ["加好友与备注短码", "B-01~B-07", "手机号搜索、固定申请语、初始备注、线下好友短码绑定、短码移除关闭自动跟进、失败原因、微信风险暂停。"],
        ["会话绑定与监听", "C-01~C-05", "会话绑定、未绑定不回复、文字监听、图片监听、销售人工回复检测。"],
        ["AI文字回复", "D-01~D-05", "普通咨询、RAG命中、敏感问题转人工、模型失败转人工、AI持续多轮接待。"],
        ["图片理解与图文回复", "E-01~E-05", "图片另存、视觉识别、图片找车、图文回复、低置信转人工。"],
        ["大风车与车源索引", "F-01~F-06", "鉴权、店铺、车辆列表、详情图片、字段隔离、接口失败降级。"],
        ["风控策略", "G-01~G-06", "总开关、静默时段、黑名单、关键词拦截、单会话限频、风险暂停。"],
        ["人工接管与飞书", "H-01~H-06", "接管状态、一次飞书通知、失败日志、不重复通知、接管后不再自动回复、销售超时提醒。"],
        ["自动召回", "I-01~I-05", "等待用户回复、召回任务、固定文案、多轮召回、排除条件。"],
        ["并发幂等恢复", "J-01~J-08", "多客户并发、同客户追加消息、sending异常、Worker重启、服务端重启、重复消息、微信UI操作串行、错误码可解释。"],
    ]
    story.append(table([["模块", "编号", "验收重点"]] + cases, [32 * mm, 28 * mm, 110 * mm], small))

    add_section(story, "5. 通过条件", h1)
    story.append(bullet_list([
        "S1缺陷数量为0。",
        "核心链路线索、加好友、会话绑定、自动回复或人工接管可演示通过。",
        "图片链路可完成图片另存、视觉识别、图文回复或低置信转人工。",
        "大风车在已提供接口条件下可同步车源，或按降级方案验收。",
        "飞书接管通知可发送，或失败日志可见。",
        "自动召回可按规则生成并发送固定文案。",
        "防重复发送用例通过。",
        "所有失败、暂停、跳过、异常恢复都有稳定错误码、说明、建议动作和trace_id。",
        "验收证据齐全。",
    ], body))

    add_section(story, "6. 验收证据", h1)
    story.append(table([
        ["证据类型", "内容"],
        ["截图", "Worker执行台、控制面状态、微信窗口关键步骤、飞书通知结果。"],
        ["日志", "task_id、conversation_id、message_id、reply_action_id、handoff_event_id、error_code、错误说明、建议动作、trace_id。"],
        ["数据记录", "线索状态、任务状态、会话状态、sent_ack、Guard结果、RAG证据、ImageIntent。"],
        ["录屏", "端到端链路建议录屏：加好友、文字回复、图片回复、人工接管、召回。"],
        ["验收表", "每条用例记录通过、失败、遗留问题、责任人、处理结论。"],
    ], [32 * mm, 138 * mm], small))

    add_section(story, "7. 不通过处理", h1)
    story.append(table([
        ["场景", "处理方式"],
        ["出现S1缺陷", "暂停验收，修复后重新验证相关用例。"],
        ["外部依赖未提供", "记录为验收前提未满足，相关用例顺延或按降级方案验收。"],
        ["业务配置未确认", "使用临时配置验收系统能力，最终配置由项目方后续调整。"],
        ["模型回答质量争议", "以知识库、Guard规则、低置信转人工机制和样本集复测为准。"],
        ["微信账号/版本异常", "记录环境异常，恢复账号或版本后重测，不直接归为系统缺陷。"],
    ], [42 * mm, 128 * mm], small))

    add_section(story, "8. 签字确认项", h1)
    story.append(p("验收日期、验收环境、微信桌面端版本、Worker版本、控制面版本、测试销售微信、验收用例总数、通过数量、S1缺陷数量、S2遗留数量、降级验收项、甲方确认人、乙方确认人。", body))

    def header_footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("STSong-Light", 8)
        canvas.setFillColor(colors.HexColor("#6B7280"))
        canvas.drawString(12 * mm, 287 * mm, f"{PROJECT_NAME} 验收标准 {VERSION}")
        canvas.drawRightString(198 * mm, 9 * mm, f"第 {canvas.getPageNumber()} 页")
        canvas.restoreState()

    doc.build(story, onFirstPage=header_footer, onLaterPages=header_footer)


def main() -> None:
    generate_pdf()
    print(PDF_PATH)


if __name__ == "__main__":
    main()
