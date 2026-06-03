from pathlib import Path


BASE = Path(__file__).resolve().parent
V25 = BASE / "AI智能客服售前跟进系统_技术方案手册_v2.5_正式工程版.md"
V23 = BASE / "AI智能客服售前跟进系统_技术方案手册_v2.3_详细设计全量版.md"
OUT = BASE / "AI智能客服售前跟进系统_技术方案手册_v2.6_正式工程完整合并版.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_from_heading(text: str, heading: str) -> str:
    idx = text.find(heading)
    if idx == -1:
        raise ValueError(f"heading not found: {heading}")
    return text[idx:].strip()


v25 = read(V25)
v23 = read(V23)

v23_detail = extract_from_heading(v23, "## 2. 模块1：云端业务控制面")

merged = f"""# AI智能客服售前跟进系统 技术方案手册（正式工程完整合并版）

版本：v2.6

日期：2026-06-02

文档定位：当前技术方案完整执行版。

合并来源：

- `v2.5_正式工程版`：作为最新状态机、Worker/服务端分工、备注短码和正式工程技术栈口径。
- `v2.3_详细设计全量版`：作为模块级详细设计基线，补回云端控制面、Worker执行台、线索销售分配、加好友、会话监听、AI对话、图片理解、车源索引、风控、飞书、召回、测试部署等详细内容。

合并原则：

1. 若 v2.5 与 v2.3 对状态机存在冲突，以 v2.5 为准。
2. 若 v2.5 未覆盖模块细节，继承 v2.3 的详细设计。
3. 若本期最新项目口径与历史技术方案冲突，以项目管理最新口径为准：抖音线索本期先人工导入，抖音 API 自动接入下一期。
4. 本文档用于研发拆解、测试验收和交付沟通；历史版本仅作追溯。

## 0. 当前项目口径补充

| 事项 | 当前口径 |
|---|---|
| 抖音线索获取 | 本期先人工导入 |
| 抖音开发者开放平台 | 已审核通过 |
| 抖音 API 自动接入 | 下一期再做 |
| 企业私信 Webhook/OAuth | 下一期再做 |
| 小风车/巨量引擎自动同步 | 下一期再做 |
| 本期线索重点 | 人工录入/导入、字段映射、去重校验、导入结果反馈、线索分配和跟进 |

## 第一部分：v2.5 最新正式工程口径

{v25.strip()}

## 第二部分：v2.3 全量模块详细设计补充

说明：以下内容来自 v2.3 详细设计全量版。凡与第一部分 v2.5 状态机、任务分工、备注短码和当前项目口径冲突的地方，以第一部分和当前项目口径为准；未冲突部分作为当前完整技术方案的细化要求。

{v23_detail}
"""

OUT.write_text(merged, encoding="utf-8")
print(OUT)
