# OmniAuto 项目结构说明

这个文件解释每一层目录负责什么，以及新任务默认应该落到哪里。

## 顶层分区

- `platform/`
  - 稳定基础设施层
  - 放核心程序、自动化测试、工具和平台技术文档
- `workflows/`
  - 任务执行层
  - 放临时任务、生成任务、验收脚本、示例和归档
- `knowledge/`
  - 项目记忆层
  - 放任务记录、模式、经验、能力说明、候选升级项和索引
- `skills/`
  - 正式复用层
  - 只放用户明确批准升级的 Skill 说明
- `docs/`
  - 仓库治理文档层
  - 放知识流转和目录规则这类仓库级说明
- `runtime/`
  - 运行产物层
  - 放数据、输出、测试与调试痕迹
- `.agents/skills/`
  - AI 运行时 Skill 资产
  - 这是工具链约定目录，继续保留在根目录

## 兼容残留

- `scripts/`
  - 冻结的兼容保留目录
  - 只用于兜住历史路径，不再接收新的任务脚本或平台资产

## platform 目录

- `platform/src/omniauto/`
  - OmniAuto 程序本体
  - 包含状态机、浏览器引擎、视觉引擎、恢复机制、模板系统、服务层
- `platform/tests/`
  - `pytest` 自动化测试
  - 包含 `unit/`、`integration/`、`e2e/`
- `platform/tools/`
  - 平台依赖工具、诊断工具、维护工具
- `platform/docs/`
  - 平台技术文档
  - 例如能力说明、架构说明、开发指南、Agent 约束

## workflows 目录

- `workflows/temporary/`
  - 一次性、探索性、偶尔使用的任务脚本
- `workflows/generated/`
  - AI 或模板自动生成的任务脚本
- `workflows/verification/`
  - 真实环境验收脚本
- `workflows/examples/`
  - 参考示例
- `workflows/archive/`
  - 历史脚本归档

## knowledge 目录

- `knowledge/tasks/`
  - 每个任务或任务族一份记录
- `knowledge/patterns/emerging/`
  - 新提炼出的模式，已可参考但还未稳定
- `knowledge/patterns/reusable/`
  - 已经比较稳定、建议复用的模式
- `knowledge/lessons/`
  - 长期有效的经验与坑点
- `knowledge/capabilities/observed/`
  - 已被任务和证据支持的能力说明
- `knowledge/capabilities/candidate/`
  - 值得进一步 formalize 的候选能力
- `knowledge/proposals/skill_candidates/`
  - 等待用户决定是否升级成 Skill
- `knowledge/proposals/platform_candidates/`
  - 等待用户决定是否进入 platform
- `knowledge/index/`
  - 任务索引、能力矩阵、知识注册表
- `knowledge/_templates/`
  - 任务结项和知识沉淀模板

## runtime 目录

- `runtime/data/`
  - 认证、浏览器 profile、报告、日志
- `runtime/outputs/`
  - 任务输出结果
- `runtime/test_artifacts/`
  - 测试、验收、调试过程文件

## 新任务的默认落点

按现在的规则：

1. 新任务脚本先放 `workflows/temporary/` 或 `workflows/generated/`
2. 调试和验证痕迹放 `runtime/test_artifacts/`
3. 正式输出放 `runtime/data/` 或 `runtime/outputs/`
4. 任务总结、模式和经验自动沉淀到 `knowledge/`
5. 只有用户明确要求时，才升级到 `skills/` 或 `platform/`
