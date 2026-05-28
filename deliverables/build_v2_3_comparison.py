from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from build_delivery_artifacts import PROJECT_NAME, ROOT, bullet_list, make_table, p


VERSION = "v2.3"
DOC_DATE = "2026-05-25"
MD_PATH = ROOT / f"{PROJECT_NAME}_技术方案手册_v2.2到v2.3_优化对比.md"
PDF_PATH = ROOT / f"{PROJECT_NAME}_技术方案手册_v2.2到v2.3_优化对比.pdf"


ROWS = [
    (
        "一期范围",
        "已包含加好友、文字回复、图片回复、大风车、风控、飞书、召回，但风险边界相对分散。",
        "一期范围不缩减；把风险转化为工程约束、验收分类和降级规则。",
        "保持既定范围，同时降低交付歧义。",
    ),
    (
        "二期SaaS化",
        "不做事项中提到多商户、复杂权限，但容易被误读为后续方案已覆盖。",
        "明确本方案不覆盖二期SaaS化、多商户、复杂权限和计费体系设计，仅作为未来扩展边界。",
        "避免把未来产品化内容混入一期验收。",
    ),
    (
        "Worker微信风险",
        "说明Worker负责微信桌面端操作，但对版本变化、卡死、异常暂停描述不够硬。",
        "新增微信版本锁定、兼容性矩阵、UI锁租约、看门狗、截图留痕、人工降级。",
        "把最大风险点变成可监控、可暂停、可恢复的工程对象。",
    ),
    (
        "状态机",
        "列出主要状态，但状态进入、退出、允许动作、禁止动作不完整。",
        "新增主状态机矩阵，覆盖new到closed、watching、handoff_required、human_active、rejected等状态。",
        "程序员可按矩阵实现状态校验，减少边界误判。",
    ),
    (
        "防重复发送",
        "已有不重复发送原则、reply_action_id和sent_ack说明。",
        "新增message_event、message_batch、reply_action、send_receipt、follow_up_task、handoff_event唯一约束。",
        "从原则变成数据库幂等约束。",
    ),
    (
        "发送事务",
        "说明旧action作废、Worker只执行最新action，但缺少事务顺序。",
        "新增接收消息、合并batch、生成回复、下发Worker、发送前claim、发送后ack、恢复扫描的原子流转。",
        "处理AI生成中断网、Worker重启、sending未知结果等高风险场景。",
    ),
    (
        "RAG/Guard",
        "已有RAG+关键词、Guard pass/rewrite/handoff/block设计。",
        "新增字段隔离、规则检查、模型复核、人工接管、审计记录和知识库验收口径。",
        "降低乱承诺价格/车况/金融和敏感字段泄露风险。",
    ),
    (
        "大风车",
        "已有API需求清单，但外部接口不确定性未分级。",
        "新增Gate 0接口确认项：鉴权、operationPhase、可售状态、对外价格、图片字段、增量同步、频率限制。",
        "接口不足时有本地导入/样本索引降级路径。",
    ),
    (
        "飞书通知",
        "说明飞书失败时AI仍停止、可人工重发。",
        "新增通知队列、退避重试、限流、幂等、降级规则。",
        "接管通知从简单调用变成可靠事件。",
    ),
    (
        "测试验收",
        "已有P1-P6测试阶段和S1缺陷。",
        "新增S1/S2/S3/外部依赖分级和并发、断网、重启、sending未知、飞书失败、大风车失败等必测用例。",
        "验收口径更像工程交付，减少争议。",
    ),
    (
        "待确认事项",
        "全部集中为待确认，阻塞项和配置项混在一起。",
        "拆成Gate 0阻塞项、开发可并行项、配置待确认项、联调待确认项。",
        "能判断哪些必须先确认，哪些可边开发边定。",
    ),
]


def markdown() -> str:
    lines = [
        "# AI智能客服售前跟进系统 技术方案优化对比",
        "",
        f"版本：{VERSION}",
        "",
        f"日期：{DOC_DATE}",
        "",
        "## 对比口径",
        "",
        "- 本次优化不缩减一期范围。",
        "- 本次优化不把二期 SaaS 化、多商户、复杂权限、计费体系纳入一期设计或验收。",
        "- 优化目标是把风险评估中成立的问题转化为明确工程规则、验收口径和降级路径。",
        "",
        "## 优化前后对比",
        "",
        "| 项目 | 优化前 v2.2 | 优化后 v2.3 | 交付影响 |",
        "|---|---|---|---|",
    ]
    for item, before, after, impact in ROWS:
        lines.append(f"| {item} | {before} | {after} | {impact} |")
    lines.extend([
        "",
        "## 结论",
        "",
        "v2.3 没有改变一期要做的业务模块，但把原来容易产生争议的部分改成了可开发、可测试、可验收的工程约束。核心变化是：Worker 风险显性化、状态机矩阵化、防重复发送数据库化、外部依赖 Gate 化、验收缺陷分级化。",
        "",
    ])
    return "\n".join(lines)


def make_styles():
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "Body",
        parent=styles["BodyText"],
        fontName="STSong-Light",
        fontSize=8.1,
        leading=11,
        spaceAfter=3,
        textColor=colors.HexColor("#1F2937"),
    )
    title = ParagraphStyle(
        "Title",
        parent=styles["Title"],
        fontName="STSong-Light",
        fontSize=18,
        leading=24,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#0B2545"),
        spaceAfter=8,
    )
    h1 = ParagraphStyle(
        "H1",
        parent=styles["Heading1"],
        fontName="STSong-Light",
        fontSize=12,
        leading=15,
        textColor=colors.HexColor("#1F4E78"),
        spaceBefore=8,
        spaceAfter=4,
    )
    small = ParagraphStyle(
        "Small",
        parent=body,
        fontSize=6.4,
        leading=8.4,
    )
    return body, title, h1, small


def generate_pdf() -> None:
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    body, title, h1, small = make_styles()
    doc = SimpleDocTemplate(
        str(PDF_PATH),
        pagesize=A4,
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=14 * mm,
        bottomMargin=13 * mm,
        title=f"{PROJECT_NAME} v2.2到v2.3优化对比",
    )
    story = [
        p("AI智能客服售前跟进系统 技术方案优化对比", title),
        p(f"版本：{VERSION}　日期：{DOC_DATE}", body),
        Spacer(1, 3 * mm),
        p("对比口径", h1),
    ]
    story.append(bullet_list([
        "本次优化不缩减一期范围。",
        "本次优化不把二期SaaS化、多商户、复杂权限、计费体系纳入一期设计或验收。",
        "优化目标是把风险评估中成立的问题转化为明确工程规则、验收口径和降级路径。",
    ], body))
    story.append(p("优化前后对比", h1))
    story.append(make_table(
        [[p(x, small) for x in row] for row in [["项目", "优化前v2.2", "优化后v2.3", "交付影响"]] + [list(r) for r in ROWS]],
        [22 * mm, 51 * mm, 61 * mm, 38 * mm],
        repeat=1,
        font_size=6.4,
    ))
    story.append(p("结论", h1))
    story.append(p("v2.3没有改变一期要做的业务模块，但把原来容易产生争议的部分改成了可开发、可测试、可验收的工程约束。核心变化是：Worker风险显性化、状态机矩阵化、防重复发送数据库化、外部依赖Gate化、验收缺陷分级化。", body))

    def header_footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("STSong-Light", 8)
        canvas.setFillColor(colors.HexColor("#6B7280"))
        canvas.drawString(12 * mm, 287 * mm, f"{PROJECT_NAME} 优化对比 {VERSION}")
        canvas.drawRightString(198 * mm, 9 * mm, f"第 {canvas.getPageNumber()} 页")
        canvas.restoreState()

    doc.build(story, onFirstPage=header_footer, onLaterPages=header_footer)


def main() -> None:
    MD_PATH.write_text(markdown(), encoding="utf-8")
    generate_pdf()
    print(MD_PATH)
    print(PDF_PATH)


if __name__ == "__main__":
    main()
