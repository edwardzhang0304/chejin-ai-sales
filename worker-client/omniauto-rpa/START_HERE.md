# 从哪里开始看

如果你是第一次打开这个项目，建议按下面顺序看：

1. [README.md](README.md)
2. [PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md)
3. [knowledge/README.md](knowledge/README.md)
4. [knowledge/index/task_catalog.md](knowledge/index/task_catalog.md)
5. [knowledge/index/capability_matrix.md](knowledge/index/capability_matrix.md)
6. [docs/KNOWLEDGE_WORKFLOW.md](docs/KNOWLEDGE_WORKFLOW.md)
7. [platform/docs/CAPABILITIES_AND_WORKFLOW.md](platform/docs/CAPABILITIES_AND_WORKFLOW.md)
8. [platform/src/omniauto](platform/src/omniauto/)
9. [workflows/README.md](workflows/README.md)
10. [platform/tests/README.md](platform/tests/README.md)
11. [runtime/test_artifacts/README.md](runtime/test_artifacts/README.md)
12. [skills/README.md](skills/README.md)

## 一句话理解现在的结构

- 核心程序在 [platform/src/omniauto](platform/src/omniauto/)
- 平台级自动化测试在 [platform/tests](platform/tests/)
- 用户任务脚本在 [workflows](workflows/)
- 长期任务记忆和可复用经验在 [knowledge](knowledge/)
- 面向用户的 Skill 说明在 [skills](skills/)
- AI 运行时 Skill 资产在 [.agents/skills](.agents/skills/)
- 运行时数据、输出和测试产物统一收口到 [runtime](runtime/)

## 这套系统默认怎么工作

默认链路是：

`自然语言任务 -> AI 选择已有能力 -> 生成确定性 workflow -> 程序执行 -> 任务经验沉淀到 knowledge/`

默认不会因为一次任务“看起来挺有用”就自动把内容写进 `skills/` 或 `platform/`。

## 如果你是新机器上的 AI

推荐最短接管路径：

1. [knowledge/README.md](knowledge/README.md)
2. [knowledge/index/task_catalog.md](knowledge/index/task_catalog.md)
3. [knowledge/index/capability_matrix.md](knowledge/index/capability_matrix.md)
4. [knowledge/index/knowledge_registry.json](knowledge/index/knowledge_registry.json)
5. 再按任务需要读取对应的 `knowledge/tasks/`、`knowledge/patterns/`、`knowledge/lessons/`

## 当前分层原则

- `knowledge/` 是自动成长的软升级层
- `skills/` 是用户批准后的正式能力包
- `platform/` 是用户批准后的长期基础设施层
- `workflows/temporary/` 用来收口一次性或探索性任务
- `scripts/` 只是冻结的兼容残留目录，不再放新内容

## 当前结构结论

现在这套仓库已经按统一蓝图收成了：

1. `platform/` 负责稳定基础设施
2. `workflows/temporary/` 负责一次性任务
3. `knowledge/` 负责自动成长的项目记忆
4. `skills/` 只保留用户批准的正式能力包
5. `runtime/` 负责所有可变运行产物
