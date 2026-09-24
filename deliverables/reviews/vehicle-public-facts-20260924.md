# 车辆公开字段补传：修复与自查记录

日期：2026-09-24。车金基线 `c496156`，分支 `codex/gray-release-0.9.x`；共享基线 `a834892`，分支 `codex/gray-release-0.9.x-source`。本记录原始自查时点未提交。用户随后授权将本修复提交推送至上述原灰度分支；未打包或部署。

共享修复来源提交：`36cd2d4d1ea6f1df04b91bc2752878484925fe1a`；车金通过 `.chejin-source.json` 登记本次选择性集成，已发布 `current_release` 保持原样。

## 根因与修改

后台已保存首次上牌等资料，但保存时生成的旧 `specs` 未包含六项公开字段。AI证据只读取 `specs`，没有把 `additional_details` 的公开字段投影进去。后续精简又只保留既有证据字段，因此模型并非没理解“上牌日期”，而是没有收到具体值。

在 `vehicle_public_facts.vehicle_specs_for_evidence` 统一读取六项白名单：`first_registration`、`mileage_km`、`exterior_color`、`interior_color`、`location`、`customer_description`。候选目录和检索片段均调用它，再通过原 `specs` 合同送入Brain。仅检索命中时也保留完整 `specs`，避免420字中间压缩再次截断资料。六项值均源自原商品，未用年款推测上牌时间，未插入客户话术。

已有数据库记录无需重存或回填；读取前后payload相同。VIN、车牌、收购价、内部备注、未知附加字段不传给AI。空值不补造；0公里保留；5000字描述末尾保留。一般来源保持旧行为，共享仓独有的大风车及置换分支未覆盖重写。

回复所有权遵守 `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/docs/customer_visible_reply_ownership_baseline.md`；共享对应 `apps/wechat_ai_customer_service/docs/customer_visible_reply_ownership_baseline.md`。旧检索 `spec` 保留；`specs` 和来源标记是可选扩展，没有改外部API、Schema、版本、配置或Worker发送流程。

## 验证与边界

本次提交归档主要原始XML及检查汇总于同级 `vehicle-public-facts-20260924-evidence/`。自查时点源码指纹保留，提交时仅补文档状态、证据归档及来源提交登记；业务代码未变。

原始结果目录：`/private/tmp/cj-vehicle-fields-fix-20260924/`。字段诊断与只读生产核对：`/private/tmp/cj-first-registration-20260924/`。

| 检查 | 结果及证据 |
| --- | --- |
| 修复前反例 | 新投影用例7失败、4对照通过，`unit-before.xml`；真实HTTP现存车辆用例因缺少首次上牌失败，`before.xml`。未改断言。 |
| 车金/共享最终投影 | 各11通过，`unit-after.xml`、`shared.xml`；覆盖目录、仅检索、合并路径，普通/精简、空值、同名不同车ID、未知及内部字段。 |
| HTTP完整字段链 | 22通过，`http-final.xml`；真实车辆管理API、PostgreSQL、知识读取、正式Brain与最终Provider HTTP请求。盘点全部19个可编辑字段，9项原公开字段保留、6项补齐、4项内部字段隔离。 |
| 最终上牌问句 | 2通过，`http-question.xml`，属于上述22项的受影响子集复跑，不重复计数。受控模型只从实际收到的规格提取日期，正式事实校验允许对应回复。 |
| 后端回归 | `regression-pg.xml` 196通过、23初始化错误；修正专用库名称及运行时路径后，`intersections-final.xml` 对应23通过，合计219通过。含车辆CRUD、知识桥接、意图/上下文、车辆修改/下架与发送凭据、图片顺序保护。 |
| Brain合同/权威来源 | `brain-contract.json` 209通过，`authority.json` 7通过；原有受控检查，非真实模型。 |
| 共享原有边界 | `shared-boundary-current.json`：8通过、1失败；`shared-boundary-baseline.json` 旧代码同一失败：城市省份回退预期“江苏南京”却为None，发生于修改路径前。此项未算通过，不在本轮修复范围。 |

环境排错保留：初次Python环境缺pytest、图像依赖或沙箱禁止监听；之后补用现有系统依赖并在隔离本机环境运行。两项交叉回归先因测试数据库安全命名要求停止，随后因默认运行时 `/app/omniauto-rpa` 不存在失败；最后指定本轮实际候选运行时，23项全部通过。未删除失败记录、降低断言或修改门禁。

本机使用独立测试容器及两份隔离测试库，不操作生产数据。HTTP里的模型端为受控替身，不能证明真实模型一定正确回答；未做Windows微信实发。本次通过的是六项字段补传及相关源码回归，不宣称共享仓全部检查通过或现场已经恢复。

## 自查

三个生产文件承担公共投影、检索入口、目录及精简入口；未扩大到其他业务流程。车金/共享新增公共模块与测试一致，旧共享独有分支保留。已检查字段隔离、旧数据无写入、空值与0、长描述、同名车身份、普通及精简最终请求；修复前反例证明可以捕获遗漏。

详细哈希、测试计数和已知边界见原始结果目录 `verification.json`。四份0.9.98文档记为r4源码候选，未修改已发布包版本或冻结发布材料。
