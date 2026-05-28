from __future__ import annotations

from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Flowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer

from build_delivery_artifacts import (
    ArchitectureDiagram,
    PROJECT_NAME,
    ROOT,
    UNIT_PRICE,
    bullet_list,
    make_table,
    p,
)


DOC_VERSION = "v1.8"
DOC_DATE = "2026-05-24"
PDF_PATH = ROOT / f"{PROJECT_NAME}_技术方案手册_{DOC_VERSION}.pdf"


class FullFlowUMLDiagram(Flowable):
    def __init__(self, width=160 * mm, height=112 * mm):
        super().__init__()
        self.width = width
        self.height = height

    def draw(self):
        c = self.canv
        lanes = [
            ("控制面", 0),
            ("Worker执行台\n商家电脑", 32),
            ("微信桌面端\n销售微信号", 64),
            ("OmniAuto\n视觉/车源", 96),
            ("销售手机端", 128),
        ]
        lane_w = 32
        c.setFont("STSong-Light", 7.5)
        for label, x in lanes:
            c.setStrokeColor(colors.HexColor("#C7D2E0"))
            c.setFillColor(colors.HexColor("#F7FAFC"))
            c.rect(x * mm, 0, lane_w * mm, 108 * mm, fill=1, stroke=1)
            c.setFillColor(colors.HexColor("#1F4E78"))
            for i, part in enumerate(label.split("\n")):
                c.drawCentredString((x + lane_w / 2) * mm, (103 - i * 4) * mm, part)

        steps = [
            (6, 92, "小风车手机号线索入库"),
            (6, 80, "分配销售\n生成加好友任务"),
            (38, 80, "领取任务"),
            (70, 68, "手机号搜索\n发送申请\n写初始绑定备注"),
            (70, 55, "客户通过/发消息"),
            (38, 55, "监听消息\n图片另存"),
            (102, 43, "理解意图\n查知识/车源\n生成候选回复"),
            (102, 31, "Guard检查"),
            (70, 20, "安全则发送AI回复"),
            (134, 8, "风险/高意向/\n销售发言后人工接管"),
        ]
        box_w = 24
        box_h = 8
        for x, y, label in steps:
            c.setStrokeColor(colors.HexColor("#2E74B5"))
            c.setFillColor(colors.HexColor("#EAF2F8"))
            c.roundRect(x * mm, y * mm, box_w * mm, box_h * mm, 2 * mm, fill=1, stroke=1)
            c.setFillColor(colors.HexColor("#0B2545"))
            c.setFont("STSong-Light", 6.4)
            for i, part in enumerate(label.split("\n")):
                c.drawCentredString((x + box_w / 2) * mm, (y + 5.2 - i * 3.1) * mm, part)

        arrows = [
            (18, 92, 18, 88),
            (18, 80, 38, 84),
            (50, 80, 70, 72),
            (82, 68, 82, 63),
            (70, 59, 50, 59),
            (50, 55, 102, 47),
            (114, 43, 114, 39),
            (102, 35, 82, 24),
            (114, 31, 134, 12),
            (82, 20, 82, 16),
        ]
        c.setStrokeColor(colors.HexColor("#64748B"))
        c.setLineWidth(0.8)
        for x1, y1, x2, y2 in arrows:
            c.line(x1 * mm, y1 * mm, x2 * mm, y2 * mm)
            c.circle(x2 * mm, y2 * mm, 0.8 * mm, fill=1, stroke=0)

        c.setFillColor(colors.HexColor("#B91C1C"))
        c.setFont("STSong-Light", 6.6)
        c.drawString(2 * mm, 2 * mm, "约束：转人工状态以系统内部状态为准；Worker停止自动回复，并通过飞书机器人通知销售手机端。")


def make_styles():
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "CNBodyV11",
        parent=styles["BodyText"],
        fontName="STSong-Light",
        fontSize=9.2,
        leading=13.2,
        spaceAfter=4,
        textColor=colors.HexColor("#1F2937"),
    )
    small = ParagraphStyle(
        "CNSmallV11",
        parent=body,
        fontSize=7.8,
        leading=10.6,
        spaceAfter=2,
    )
    title = ParagraphStyle(
        "CNTitleV11",
        parent=styles["Title"],
        fontName="STSong-Light",
        fontSize=22,
        leading=28,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#0B2545"),
        spaceAfter=10,
    )
    subtitle = ParagraphStyle(
        "CNSubtitleV11",
        parent=body,
        fontSize=11,
        leading=16,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#4B5563"),
        spaceAfter=12,
    )
    h1 = ParagraphStyle(
        "CNH1V11",
        parent=styles["Heading1"],
        fontName="STSong-Light",
        fontSize=14.2,
        leading=18,
        textColor=colors.HexColor("#1F4E78"),
        spaceBefore=7,
        spaceAfter=5,
    )
    h2 = ParagraphStyle(
        "CNH2V11",
        parent=styles["Heading2"],
        fontName="STSong-Light",
        fontSize=11.2,
        leading=14.5,
        textColor=colors.HexColor("#2E74B5"),
        spaceBefore=5,
        spaceAfter=3,
    )
    code = ParagraphStyle(
        "CNCodeV11",
        parent=body,
        fontName="STSong-Light",
        fontSize=8,
        leading=10.5,
        backColor=colors.HexColor("#F4F6F9"),
        borderColor=colors.HexColor("#E5E7EB"),
        borderWidth=0.35,
        borderPadding=5,
        spaceBefore=3,
        spaceAfter=5,
    )
    return body, small, title, subtitle, h1, h2, code


def table(data, widths, style, repeat=1, font_size=7.8):
    return make_table([[p(str(c), style) for c in row] for row in data], widths, repeat=repeat, font_size=font_size)


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
    story.append(Spacer(1, 14 * mm))
    story.append(p(PROJECT_NAME, title))
    story.append(p("技术方案手册（软件项目交付标准版）", subtitle))
    cover = [
        ["项目定位", "抖音小风车手机号线索驱动的个人微信AI销售预跟进系统"],
        ["文档版本", DOC_VERSION],
        ["文档日期", DOC_DATE],
        ["预算基线", f"87,000元（58人天 × {UNIT_PRICE}元/人天）"],
        ["实施周期", "6-8周"],
        ["范围基线", "第一期正式工程版本；后续SaaS化、复杂权限、计费和高可用另行评估"],
        ["交付口径", "按软件项目标书式交付：范围、质量、验收、缺陷、变更、交付物均可追踪"],
    ]
    story.append(table(cover, [35 * mm, 123 * mm], body, repeat=0, font_size=9))
    story.append(Spacer(1, 7 * mm))
    story.append(p("本手册用于立项、开发、测试、灰度、验收和后续变更管理。除双方书面确认外，本文档与报价单共同构成本期正式工程交付基线。", body))
    story.append(PageBreak())

    story.append(p("1. 项目目标与第一性原理", h1))
    story.append(p("本项目的本质不是建设一个通用聊天机器人，而是建设一个“线索到销售接管”的工程系统。AI的职责是提升首次响应、暖场、识别意向和减少无效沟通；最终成交仍由真人销售完成。", body))
    story.append(bullet_list([
        "业务目标：将抖音小风车手机号线索更快、更稳定地推进到销售接管。",
        "工程目标：让线索状态、会话绑定、AI回复、图文识别、风控接管和失败追踪形成闭环。",
        "设计原则：保留必要复杂度，避免把一期工程版本扩展成完整SaaS、复杂坐席系统或通用AI平台。",
        "边界原则：模型只生成候选表达和理解结果，业务事实、敏感字段和最终发送权必须受系统规则约束。",
    ], body))

    story.append(p("2. 一期正式工程范围基线", h1))
    scope = [
        ["类别", "内容", "验收口径"],
        ["包含", "手机号线索导入、销售分配、个人微信手机号加好友、备注绑定、文字AI回复、图片另存、视觉理解、图文自动回复、本地车源索引、大风车基础适配、销售接管、自动召回、日志与失败重试", "以端到端闭环演示和验收用例通过为准"],
        ["不包含", "完整SaaS、多租户计费、复杂权限、抖音官方API、复杂BI、模型微调平台、高可用集群、长期运维、完整坐席系统", "不作为本期验收项；新增时走变更流程"],
        ["甲方前提", "商家侧测试电脑、测试销售微信、销售微信手机客户端、小风车线索样本、大风车参数、千问视觉API Key、销售接管规则、图片测试样本", "未按期提供时，交付周期相应顺延"],
    ]
    story.append(table(scope, [23 * mm, 91 * mm, 44 * mm], small))

    story.append(p("3. 质量属性与非功能要求", h1))
    quality = [
        ["质量属性", "工程要求", "一期验收基线"],
        ["功能性", "覆盖线索导入、分配、加好友、会话绑定、文字回复、图片回复、转人工、审计", "A类验收用例通过；核心流程可重复演示"],
        ["性能效率", "实时对话链路不做重型同步外部依赖；车源查询优先本地索引", "性能数值仅作为优化目标，不作为未经压测的硬验收承诺；最终以测试环境实测数据为准"],
        ["兼容性", "明确Windows、微信桌面端、模型API、大风车API的适配边界", "指定测试环境通过；版本升级需回归测试"],
        ["易用性", "销售使用微信手机客户端正常接管；Worker使用商家侧电脑上的微信桌面客户端执行自动化；管理员可查看任务、失败原因和基本状态", "无需销售理解AI后台即可使用"],
        ["可靠性", "任务幂等、失败重试、Worker心跳、断点恢复、模型失败降级", "灰度验收时S1缺陷为0；失败任务可追踪"],
        ["安全性", "手机号、图片、密钥、车主隐私和底价信息分级处理", "敏感字段不进入AI可见上下文；日志不明文记录密钥"],
        ["维护性", "模块边界清晰，配置、Prompt、规则、接口适配分层", "新增模型/车源适配不改动核心状态机"],
        ["可移植性", "本地Worker、模型Provider、车源Provider通过适配层隔离", "更换模型或部署机器时保留主业务状态"],
    ]
    story.append(table(quality, [22 * mm, 88 * mm, 48 * mm], small))
    story.append(p("性能指标口径", h2))
    story.append(p("本期正式工程版本的核心交付标准是系统能根据客户消息正常回复，或在无法安全回复、识别失败、命中风险规则时触发人工接管。文字回复、图片回复和管理页查询的具体耗时可作为灰度阶段优化目标，但不作为未经压测的硬性验收承诺，最终以测试环境、账号状态、网络质量、模型服务和真实样本的实测数据为准。", body))

    story.append(p("4. 总体架构", h1))
    story.append(ArchitectureDiagram())
    story.append(Spacer(1, 4 * mm))
    story.append(p("系统保持“业务控制面 + 本地RPA Worker + OmniAuto AI引擎 + 图片理解 + 车源索引 + Guard接管”的六层边界。该设计的核心是让微信操作、AI生成、业务状态和风控规则互不混杂，便于测试、排障和后续扩展。", body))
    story.append(p("主流程", code))
    story.append(p("手机号线索 -> 销售分配 -> 加好友任务 -> 微信备注绑定 -> 会话监听 -> OmniAuto生成候选回复 -> Guard检查 -> 自动回复或人工接管 -> 审计记录", code))

    story.append(p("5. UML全流程", h1))
    story.append(p("下图采用UML活动图口径表达全流程。销售人工接管发生在微信手机客户端；Worker运行在商家侧电脑，控制同一销售微信号登录的微信桌面客户端。两端共用同一微信账号，因此转人工后Worker必须停止自动回复和主动操作，避免与销售手机端人工回复形成并发冲突。", body))
    story.append(FullFlowUMLDiagram())

    story.append(p("6. 模块边界与职责", h1))
    modules = [
        ["模块", "主要职责", "不得承担的职责"],
        ["业务控制面", "线索、销售、任务、状态、审计、配置", "不直接操作微信UI"],
        ["本地Worker", "承载加好友任务类型、聊天回复任务类型、自动召回任务类型、微信UI锁、心跳上报和本机执行台", "不保存业务主状态，不把不同任务类型的业务逻辑耦合"],
        ["OmniAuto AI引擎", "上下文管理、RAG、候选回复生成、图文回复编排", "不直接读取底价/车主隐私，不绕过Guard发送"],
        ["AI Reply Provider Adapter", "预留Dify/FastGPT/其他模型工作流接入", "不作为第一期业务主状态库"],
        ["图片/视觉模块", "图片保存、视觉识别、结构化ImageIntent", "不直接生成最终客服话术"],
        ["车源服务", "同步大风车、清洗字段、建立本地索引、字段白名单", "不把敏感价格和车主隐私传给AI"],
        ["Guard/接管", "风险判断、停止规则、转人工、发送前校验", "不依赖单一Prompt判断高风险"],
    ]
    story.append(table(modules, [29 * mm, 76 * mm, 53 * mm], small))
    story.append(p("Worker任务类型拆分", h2))
    worker_types = [
        ["Worker任务类型", "只负责的业务", "不负责的业务"],
        ["add_friend", "领取加好友任务、手机号搜索、发送好友申请、写入初始绑定备注、回传申请结果", "不监听客户对话，不调用AI生成回复，不判断客户意向"],
        ["chat_reply", "监听已绑定会话、读取文字/图片、图片另存、调用OmniAuto、Guard检查、发送AI回复、触发人工接管、触发飞书机器人通知", "不批量加好友，不修改转人工备注，不处理线索分配"],
        ["follow_up", "按召回规则生成再触达任务，发送固定召回文案，记录召回结果", "不负责实时聊天回复，不自行判断成交，不绕过风控策略"],
        ["Local WeChat UI Lock", "同一台商家电脑、同一个微信桌面客户端的所有UI操作必须串行执行", "不是业务模块，不决定客户状态；只负责避免两个任务类型抢微信窗口"],
    ]
    story.append(table(worker_types, [34 * mm, 74 * mm, 50 * mm], small))
    story.append(p("拆分原则", h2))
    story.append(bullet_list([
        "两个任务类型可以运行在同一个Worker进程或同一台商家侧电脑上，第一期不要求物理拆分。",
        "两个任务类型使用独立任务队列和独立状态机，业务逻辑互不依赖。",
        "两个任务类型共用同一个Local WeChat UI Lock；只有真正操作微信桌面端时需要抢锁。",
        "非UI工作如AI生成、图片识别、车源查询、日志记录可以并行执行。",
    ], body))

    story.append(p("7. 本地Worker可视化执行台", h1))
    story.append(p("Worker不应设计成完全不可见的后台脚本。第一期应参照“微信桌面客户端 + 本地执行台”的形态：商家侧电脑打开该销售微信号登录的微信桌面客户端，旁边展示Worker执行台；销售人工接管主要通过自己的微信手机客户端完成。执行台展示当前客户、任务步骤、视觉截图、AI处理结果、发送结果和运行控制。", body))
    worker_ui = [
        ["区域", "内容", "验收口径"],
        ["任务状态", "显示当前线索、绑定销售、微信会话、执行阶段、成功/失败状态", "销售或管理员能判断当前Worker正在做什么"],
        ["步骤时间线", "按顺序展示检查微信、读取消息、图片另存、视觉识别、回复生成、发送完成、转人工、飞书通知等步骤", "每一步有状态和时间，不只显示最终结果"],
        ["画面证据", "保留当前微信窗口截图或图片缩略图，用于确认识别对象", "排障时能看到当时处理的是哪条消息/哪张图片"],
        ["AI结果", "展示视觉理解摘要、候选回复、Guard结论、最终发送内容", "能复盘AI为什么这样回复"],
        ["运行控制", "提供启动、暂停、继续、停止、手动接管/禁用AI等按钮", "异常时可以人工停止，不依赖杀进程"],
        ["健康状态", "显示Worker心跳、微信连接状态、模型连接状态、车源同步状态", "状态异常可见"],
        ["日志入口", "显示最近错误、失败原因、任务ID和会话ID", "失败任务可追踪"],
    ]
    story.append(table(worker_ui, [24 * mm, 82 * mm, 52 * mm], small))
    story.append(p("设计约束", h2))
    story.append(bullet_list([
        "执行台只负责本机运行可视化和人工控制，不作为业务主数据库。",
        "执行台需展示当前运行的Worker任务类型，例如add_friend、chat_reply或follow_up。",
        "执行台不得展示采购价、销售底价、经理价、车主隐私和API密钥。",
        "第一期不要求做复杂坐席工作台，只要求做到本机可观察、可暂停、可恢复、可复盘。",
        "当销售通过微信手机客户端手动接管或在执行台点击停止后，该会话AI自动回复必须停止。",
    ], body))

    story.append(p("8. 核心数据流与状态机", h1))
    state = [
        ["对象", "状态流转", "关键规则"],
        ["线索", "new -> assigned -> add_friend_pending -> add_friend_sent -> friend_added -> ai_chatting -> handoff_required -> human_taken_over -> closed", "状态变更必须记录时间、触发来源和失败原因"],
        ["加好友任务", "pending -> running -> sent -> failed -> expired", "按任务ID幂等执行；失败可重试但不得重复骚扰客户"],
        ["聊天回复任务", "message_received -> preparing -> ai_generating -> guard_checking -> sent/handoff/failed", "仅处理已绑定会话；转人工后不再自动回复"],
        ["自动召回任务", "eligible -> scheduled -> waiting_window -> sending -> sent/skipped/failed", "仅对未拒绝、未人工接管、未触达上限且满足时间规则的客户执行"],
        ["会话", "unbound -> bound -> ai_active -> handoff_open -> human_active -> stopped", "检测到销售手机客户端手动发送或系统触发转人工后AI停止；人工接管状态优先级最高"],
        ["图片资产", "received -> saved -> recognized -> linked -> replied/failed", "保存路径、来源消息、识别结果和回复结果可追踪"],
    ]
    story.append(table(state, [21 * mm, 91 * mm, 46 * mm], small))

    story.append(p("9. 人工接管与通知规则", h1))
    story.append(p("微信备注仅用于初始绑定，不作为人工接管状态的依据。人工接管状态必须以系统内部会话状态为准。转人工时系统应立即关闭该会话AI自动回复，并通过飞书机器人通知对应销售的手机端，而不是通过修改微信备注来通知。", body))
    handoff_rules = [
        ["规则", "说明"],
        ["初始备注", "加好友或绑定阶段可写入短码备注，例如CJ-张三-A7K9-1234，用于线索与微信会话初始绑定。"],
        ["转人工状态", "转人工时只更新系统内部状态，如handoff_required/human_active，并立即关闭该会话AI自动回复。"],
        ["飞书通知", "系统通过飞书机器人向对应销售发送接管通知，通知内容包含客户标识、线索短码、触发原因、最近一条客户消息和建议动作。"],
        ["不改转人工备注", "转人工时不修改微信备注来表达接管状态，避免Worker在微信桌面端操作同一账号，和销售手机端人工回复形成并发冲突。"],
        ["通知失败处理", "飞书机器人通知失败时记录失败原因，并在控制面/Worker执行台展示告警；通知失败不应导致AI继续自动回复。"],
        ["验收口径", "验收以AI停止、系统接管状态正确和飞书通知触发记录为准，不以微信备注是否变更为准。"],
    ]
    story.append(table(handoff_rules, [30 * mm, 128 * mm], small))

    story.append(p("10. AI对话与RAG架构", h1))
    story.append(p("第一期以OmniAuto作为主对话控制引擎。Dify或FastGPT可作为后续候选回复Provider接入，但不直接接管业务状态、车源敏感字段和最终发送权。", body))
    ai_arch = [
        ["层次", "职责", "实现口径"],
        ["知识来源", "稳定知识、销售话术、二手车规则、车型资料、服务流程", "进入OmniAuto知识库/RAG"],
        ["动态事实", "车源、价格、里程、状态、图片", "来自大风车同步后的本地结构化索引"],
        ["候选回复", "根据上下文、知识和事实生成表达", "默认OmniAuto；可通过Adapter调用Dify/FastGPT"],
        ["发送前检查", "敏感承诺、越权报价、车况断言、金融承诺、接管触发", "由Guard执行，不能只靠模型自觉"],
        ["审计", "记录输入、召回、候选回复、Guard结论、最终动作", "用于复盘和验收"],
    ]
    story.append(table(ai_arch, [27 * mm, 66 * mm, 65 * mm], small))
    story.append(p("图文回复链路", code))
    story.append(p("客户图片 -> RPA另存 -> 视觉模型识别 -> ImageIntent -> 本地车源索引 -> evidence pack -> OmniAuto候选回复 -> Guard -> 发送/接管", code))

    story.append(p("11. 自动召回模块", h1))
    story.append(p("自动召回用于对已添加微信但处于观望状态、长期未互动且未明确拒绝的客户进行低频再触达。该模块不是实时聊天，不做AI自由发挥；第一期采用固定文案和明确规则，所有发送仍受风控策略、静默时段、每日上限、黑名单和Local WeChat UI Lock约束。", body))
    recall_rules = [
        ["配置项", "说明", "一期工程口径"],
        ["适用客户状态", "例如观望、已添加未成交、未拒绝、未人工接管", "状态由控制面维护"],
        ["触发周期", "例如7天未联系且无新消息", "周期可配置，默认规则需双方确认"],
        ["固定文案", "第一期使用固定召回话术，不让模型临场自由生成", "文案可配置并留痕"],
        ["发送窗口", "仅在允许时间段发送，避开静默时段", "命中静默则延期"],
        ["发送上限", "受每日上限、单客户召回次数上限控制", "超过上限则跳过并记录原因"],
        ["排除条件", "已拒绝、已成交、已转人工、黑名单、近期销售已联系、风险暂停", "命中任一条件不得发送"],
        ["结果记录", "记录召回任务ID、规则、文案、发送时间、成功/失败/跳过原因", "控制面可查询"],
    ]
    story.append(table(recall_rules, [27 * mm, 78 * mm, 53 * mm], small, font_size=7.5))
    story.append(p("示例规则", code))
    story.append(p("客户状态=观望 且 最近7天无客户消息/销售消息 且 未拒绝 且 未转人工 且 不在黑名单 -> 生成follow_up任务 -> 命中发送窗口后发送固定召回文案 -> 记录结果", code))

    story.append(p("12. 风控策略中心", h1))
    story.append(p("风控策略中心参考既有自动回复产品文档与Worker执行台原型补充。本期不只依赖Prompt判断风险，而是将自动回复开关、接管、静默、限额、名单、关键词、延迟和限频做成可配置规则。配置由服务端控制面管理，Worker执行台展示当前命中状态并执行服务端返回的动作。", body))
    risk_controls = [
        ["风控项", "功能说明", "一期验收口径"],
        ["自动回复总开关", "控制AI自动回复启用状态，可按全局/销售/会话配置", "关闭后Worker不发送AI回复"],
        ["人工接管模式", "会话进入handoff_required/human_active后AI停止，服务端触发飞书机器人通知销售", "AI停止且通知记录可追踪"],
        ["静默时段", "配置不自动回复的时间段，例如夜间或门店非工作时间", "静默期内不主动发送AI回复，可记录待处理或转人工"],
        ["每日上限", "限制每日自动回复数量，可按销售微信号或Worker统计", "超过上限后停止自动回复并展示原因"],
        ["黑白名单", "按手机号、线索短码、会话或客户标识控制允许/禁止自动回复", "黑名单不回复；白名单按规则放行"],
        ["关键词拦截", "拦截敏感、无关、投诉、法务、退款等关键词", "命中后不直接回复或转人工"],
        ["人工接管关键词", "命中价格底线、事故车、泡水、贷款审批、合同等关键词时触发接管", "触发handoff并发送飞书通知"],
        ["随机发送延迟", "发送前增加可配置随机延迟，使回复节奏更自然", "延迟可配置；不承诺规避微信平台风控"],
        ["风险提示检测", "检测微信桌面端出现操作频繁、环境异常等提示后暂停发送", "暂停原因在控制面/Worker执行台可见"],
        ["单会话突发限频", "限制短时间内对同一客户连续回复次数", "超过阈值后暂停该会话自动回复或转人工"],
    ]
    story.append(table(risk_controls, [28 * mm, 80 * mm, 50 * mm], small, font_size=7.5))
    story.append(p("风控执行顺序", h2))
    story.append(p("消息进入chat_reply后，先检查总开关、黑白名单、静默时段、每日上限和单会话限频，再进行关键词拦截/接管判断；通过后才调用OmniAuto生成候选回复，并在发送前再次执行Guard检查。任何风控命中都必须记录命中规则、处理动作和时间。", body))

    story.append(p("13. 安全与隐私要求", h1))
    security = [
        ["类别", "要求", "验收方式"],
        ["手机号", "界面展示可脱敏；日志避免无必要全量输出", "抽查日志和页面"],
        ["图片", "本地保存目录受控；保留周期可配置；与线索/会话建立关联", "检查配置和样本记录"],
        ["密钥", "大风车、模型API Key不得写入代码和普通日志", "代码与日志抽查"],
        ["车源敏感字段", "采购价、底价、经理价、车主姓名、身份证、银行卡不得进入AI上下文", "构造测试用例验证"],
        ["Prompt与规则", "不得向客户暴露系统提示词、内部规则和模型身份细节", "对抗样本测试"],
        ["权限", "第一期只做基础管理入口；后续多角色权限另行评估", "不作为本期复杂权限验收"],
    ]
    story.append(table(security, [25 * mm, 83 * mm, 50 * mm], small))

    story.append(p("14. 可靠性与异常恢复", h1))
    reliability = [
        ["场景", "处理策略", "验收口径"],
        ["Worker离线", "心跳超时标记离线，任务停止派发或进入待重试", "离线状态可见"],
        ["微信窗口异常", "记录异常、停止当前任务、允许人工恢复后重试", "失败原因可见"],
        ["重复消息", "按消息ID/时间/会话去重，避免重复回复", "重复样本不重复发送"],
        ["风控暂停", "命中总开关关闭、静默时段、每日上限、黑名单、风险提示或限频后暂停发送", "暂停原因可见且不继续自动回复或召回"],
        ["模型超时", "超时后重试或降级为固定话术/转人工", "超时任务不丢失"],
        ["视觉失败", "图片识别失败时使用兜底话术或转人工", "失败路径可追踪"],
        ["大风车失败", "对话优先读本地缓存；同步失败告警", "接口失败不阻塞普通对话"],
        ["销售接管", "人工接管状态优先级最高，不被AI自动恢复覆盖；Worker不修改转人工备注；系统通过飞书机器人通知销售手机端", "销售手机端发言或系统转人工后AI停止准确率100%；飞书通知触发记录可追踪"],
    ]
    story.append(table(reliability, [30 * mm, 83 * mm, 45 * mm], small))

    story.append(p("15. 兼容性、维护性与可移植性", h1))
    maintain = [
        ["维度", "要求"],
        ["运行环境", "第一期以指定商家侧Windows测试电脑和指定微信桌面版本为验收环境；微信升级需回归测试。"],
        ["模型供应商", "通过AI Reply Provider Adapter隔离模型调用，避免把模型供应商写死在业务状态机中。"],
        ["车源系统", "通过Vehicle Provider Adapter封装大风车鉴权、字段清洗和同步任务。"],
        ["配置项", "销售、微信设备、最大轮次、停止规则、召回规则、风控策略、视觉模型、车源同步周期应配置化。"],
        ["测试集", "保留文字、图片、风控、接管、车源匹配回归样本，用于版本升级。"],
        ["部署迁移", "控制面数据、知识库、车源索引、运行配置和Worker安装包分离，便于迁移到新机器。"],
    ]
    story.append(table(maintain, [28 * mm, 130 * mm], small))

    story.append(p("16. 验收标准", h1))
    acceptance = [
        ["编号", "验收项", "通过标准"],
        ["A-01", "线索导入", "可导入手机号线索并进入待分配状态"],
        ["A-02", "销售分配", "线索可分配到指定销售和对应商家侧Worker/微信桌面端"],
        ["A-03", "加好友任务", "RPA可按手机号搜索、发送固定申请语并记录结果"],
        ["A-04", "初始备注绑定", "可生成短码备注并将微信会话绑定回线索；人工接管不修改微信转人工备注"],
        ["A-05", "文字回复", "客户文字消息可正常自动回复；若命中风险规则则触发人工接管；回复过程记录审计"],
        ["A-06", "图片回复", "客户图片可另存、识别并生成图文回复；若识别失败或命中风险规则则触发人工接管"],
        ["A-07", "车源检索", "可基于本地索引返回AI可见字段，不暴露敏感字段"],
        ["A-08", "高风险接管", "价格、车况、金融、合同等高风险场景不直接越权承诺"],
        ["A-09", "自动召回", "观望客户满足7天未联系且未拒绝等规则时，可生成follow_up任务并发送固定文案；不满足条件时跳过并记录原因"],
        ["A-10", "风控策略", "总开关、静默时段、每日上限、黑白名单、关键词拦截、接管关键词、随机延迟、风险提示检测、单会话限频可配置或可验证"],
        ["A-11", "销售停止AI", "销售通过微信手机客户端手动发言后AI自动回复停止，准确率100%"],
        ["A-12", "失败追踪", "加好友、召回、图片、模型、车源、风控暂停均可查看原因"],
        ["A-13", "飞书接管通知", "系统转人工时可触发飞书机器人通知对应销售，并记录通知结果；通知失败可见且AI仍停止"],
        ["A-14", "Worker任务类型解耦", "add_friend只负责加好友；chat_reply只负责聊天回复；follow_up只负责自动召回；三者共用Local WeChat UI Lock且不同时操作微信窗口"],
        ["A-15", "Worker执行台", "可查看当前任务、Worker任务类型、步骤状态、截图证据、候选回复、Guard结论、风控命中原因，并支持暂停/停止"],
        ["A-16", "灰度验收", "核心交付标准为能正常回复或触发人工接管；试运行期间S1缺陷为0；S2缺陷修复或双方确认规避"],
    ]
    story.append(table(acceptance, [16 * mm, 47 * mm, 95 * mm], small, font_size=7.6))

    story.append(p("17. 缺陷等级、变更与交付物", h1))
    defect = [
        ["等级", "定义", "处理要求"],
        ["S1", "核心链路不可用，如无法加好友、无法监听、无法发送回复、AI无法停止", "阻塞验收，必须修复"],
        ["S2", "核心功能部分失败或存在明显业务风险，如图片链路不稳定、接管状态错误", "验收前修复或书面确认规避方案"],
        ["S3", "非核心缺陷，如界面文案、轻微日志展示问题", "不阻塞验收，进入遗留清单"],
        ["S4", "优化建议或新增需求", "不计入本期缺陷，进入变更或二期需求"],
    ]
    story.append(table(defect, [17 * mm, 88 * mm, 53 * mm], small))
    story.append(p("变更控制", h2))
    story.append(bullet_list([
        "本手册、报价单、验收清单共同构成本期范围基线。",
        "新增渠道、完整SaaS、多商户隔离、模型微调、复杂BI、长期运维等均视为范围变更。",
        "变更需记录内容、原因、影响模块、追加人天、排期影响和确认人。",
        "未经书面确认的口头需求不作为验收依据。",
    ], body))
    deliverables = [
        ["交付物", "内容"],
        ["程序与配置", "控制面、Worker、AI配置、车源同步配置、Guard规则配置、风控策略配置、召回规则配置"],
        ["测试材料", "端到端验收用例、图片样本、风控样本、回归测试结果"],
        ["部署材料", "部署步骤、环境要求、启动/停止方式、常见故障处理"],
        ["交付记录", "缺陷清单、变更记录、验收记录、遗留问题清单"],
    ]
    story.append(table(deliverables, [32 * mm, 126 * mm], small))

    story.append(p("18. 里程碑与当前不做事项", h1))
    milestones = [
        ["阶段", "周期", "交付物"],
        ["P1 启动与基础闭环", "第1-2周", "线索导入、销售分配、加好友任务、备注规则、任务状态"],
        ["P2 文字AI闭环", "第3-4周", "文字回复、上下文绑定、停止规则、审计、基础Guard、风控策略和自动召回规则"],
        ["P3 图片与图文回复", "第5-6周", "图片另存、视觉识别、ImageIntent、图文回复、回归样本"],
        ["P4 车源与灰度验收", "第7-8周", "大风车基础适配、本地索引、灰度测试、交付文档"],
    ]
    story.append(table(milestones, [31 * mm, 27 * mm, 100 * mm], small))
    story.append(p("当前不做事项", h2))
    story.append(bullet_list([
        "不做完整SaaS平台、多租户计费和复杂权限。",
        "不做大模型微调平台和GPU训练平台。",
        "不做全渠道坐席系统和复杂BI报表。",
        "不把Dify/FastGPT作为第一期业务主状态系统。",
        "不承诺规避微信平台风控或提升微信好友通过率。",
    ], body))

    story.append(p("19. 技术结论", h1))
    story.append(p("本方案保留现有OmniAuto主链路，不做推倒重构。v1.8的调整重点是把原有交付说明升级为软件项目交付标准：明确质量属性、安全边界、可靠性策略、自动召回、风控策略中心、验收口径、变更规则、Worker可视化执行边界和Worker任务类型解耦。核心交付标准为系统能正常回复、按规则自动召回或触发人工接管；人工接管以系统内部状态为准，并通过飞书机器人通知销售手机端，不通过修改微信备注通知；性能指标作为优化目标，不作为未经压测的硬承诺，最终以测试环境实测数据为准。", body))

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
