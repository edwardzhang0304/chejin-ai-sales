# Roadmap

Objective: 在原仓库中基于已确认的最简方案，先编写并审计图片内 OCR 与微信图片消息仲裁的开发文档；文档审计通过后，以最小改动修复真实汽车图片中车牌等图片内文字被误判为 text_bubble、从而不触发右键复制和 Vision 的问题；补充真实回归测试，完成代码审计、定向及相关全量测试，失败则迭代修复，最终提交并推送到原 GitHub 仓库的 PR。保持现有外部合同、框架、后端接口、Vision API 和 RPA 流程不变，不修改 VPS 或现场数据。

Phase status: implementation and layered verification complete; delivery pending.

