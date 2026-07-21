# C2 OmniAuto / Worker / 后端一致性审计

- 审计日期：2026-07-14（Asia/Shanghai）
- 审计范围：OmniAuto Win32/OCR 消息解析与语音转写、Worker C2 消息归一化与上报、后端消息校验/去重/入库/业务副作用、现有单元测试与本地数据库事实
- 本轮动作：只审计，不修改业务代码

## 一、结论先行

C2 当前不是“某个 OCR 条件偶尔不准”，而是三层没有真正统一消息契约。系统同时存在至少四套消息身份：OmniAuto `id/canonical_visual_id`、语音 `voice_anchor_stable_key`、Worker `dedupe_key`、后端 voice fallback key。三层也都会重新判断消息类型、发送方和语音状态。

因此，同一条微信消息可以在一层被解释为 `text/self`，在另一条支路被解释为 `voice/customer`，再生成两个不同的 `dedupe_key`。后端只按 `dedupe_key` 去重，最终会把两条都存下来，并分别触发客户入站或销售出站业务副作用。

本地数据库已存在 6 组确定证据：同一 `raw_payload.id`、同一 `canonical_visual_id`，却对应两个不同 `dedupe_key`；其中包含 `text/voice` 和 `customer/self` 冲突。这证明问题是架构性缺口，不是上一轮单点偶发。

## 二、已证实的阻断问题

### P0-1 同一条消息有多个语义裁判

**证据链**

1. OmniAuto 从 OCR 行生成消息，并先判断 `sender_role`、`type`。
2. `messages` 与 `voice_transcription.transcribed_messages` 是两路独立结果。
3. Worker 先遍历 `messages` 直接追加，再把没有匹配上的转写结果作为新的 voice 追加。
4. 后端再次归一化 `audio -> voice`，并可能重算 voice 去重键。

**本轮失败的直接形成过程**

```text
同一视觉消息
-> messages 支路：text/self
-> voice_transcription 支路：voice/customer
-> Worker 两条都追加，并生成不同 dedupe_key
-> 后端只看到两个不同 dedupe_key
-> 两条都入库
```

**影响**

- 同一语音同时成为 text 和 voice。
- customer/self 冲突会污染会话方向判断。
- customer 记录可能触发 C3 入站处理；self 记录会关闭 AI 并标记销售已回复。同一事实可能触发相反副作用。

### P0-2 OmniAuto 已算出“语音转文字续行”，但分组条件把它挡掉

OmniAuto 解析器已经计算：上一行是语音时长、下一行不是语音时长、下一行没有头像、垂直间距符合，这就是语音转文字续行。

但最终合并还额外要求 `previous_side == side`。语音行可通过同排头像判为 customer；下方转写行没有头像，会被几何位置猜成 self。结果是结构关系成立，却因为两个临时角色不同而不合并。

这正是“左侧 customer 语音 + 下方灰色转写文本”被拆成 `voice/customer` 与 `text/self` 的代码根因。

### P0-3 所谓 stable/canonical ID 含有会变化的字段

当前 `canonical_visual_id` 的种子包含：发送方角色、内容、OCR message id、bubble id、矩形坐标。语音转写后内容会变化、页面会抖动、角色可能被修正、坐标会移动，因此该 ID 不是跨读取稳定身份。

语音 `voice_anchor_stable_key` 也包含 `x/y bucket`。它只对很小的位移容忍，滚动、展开文本、窗口尺寸变化后会改变。

结果是：

- 同一条消息可能跨轮生成新身份并重复入库。
- 两条相同时长、相近位置的语音又可能被错误视为同一锚点。
- 身份中包含 `role/type/content`，导致一旦分类修正，身份也跟着变，后端无法识别这是同一来源的冲突版本。

### P0-4 后端没有“来源消息身份”字段和唯一约束

后端请求模型只正式接收 `dedupe_key/sender_role_hint/message_type/content/raw_payload`。`source id`、`canonical_visual_id`、voice anchor、契约版本和判定证据都藏在未类型化的 `raw_payload` 中。

数据库唯一约束只有：

```text
unique(conversation_id, dedupe_key)
```

所以后端不能表达也不能强制以下规则：

```text
同一 read_run 内，同一来源消息只能产生一条最终消息。
```

数据库事实：123 条 `message_events` 中，有 9 条缺少 source/canonical identity；已有 6 组相同 source ID 和 canonical visual ID 对应多个 dedupe key。

### P0-5 固定总时长仍在充当正常流程裁判

当前 sidecar 每次循环先判断从 flow 开始后的总耗时，达到 `max_duration_seconds` 就停止。这个计时不会因为“菜单已打开、转写成功一条、正在处理下一条”等进展而重置。Worker 外层还有一个固定进程超时。

因此当前实现仍不是此前认可的“无进展 watchdog”，而是“整个流程总时长上限”。语音较多或 OCR 较慢时，即使持续有进展，也会被切成 `partial`。

### P0-6 `partial` 在三层语义不一致

- OmniAuto：`partial` = 已成功一部分，但屏幕内仍有未转写语音。
- Worker：把 `partial` 放在 success states，继续最终读取和上报。
- 后端：没有正式 flow/item 状态字段，只看单条是否有文本。

“成功的单条允许上报”本身符合已确认口径，但必须同时明确：

- flow 是 partial，不得伪装成完整完成；
- 只上报 `item_state=completed` 的语音；
- 最终普通 OCR 文本不得再次消费这些已绑定转写行；
- 未完成项必须有独立失败结果，不能靠质量标签猜。

当前契约没有表达这四件事。

## 三、其他一致性风险

### P1-1 角色枚举不一致

- OmniAuto 会产生 `group_member/contact/self/customer/unknown` 等。
- Worker 接受 `self/sales/sales_candidate/customer/unknown`，把 contact 映射 customer，但 group_member 变 unknown。
- 后端接受 `customer/self/sales/sales_candidate/unknown`。

C2 私聊应在边界统一成 `customer/self/system/unknown`；`sales/sales_candidate/contact` 只作为输入别名，不应继续进入持久层。群聊如果不在 C2 范围，应显式拒绝，而不是悄悄变 unknown。

### P1-2 消息类型枚举不一致

- Worker 接受 `voice/audio/video`。
- 后端把 audio 转 voice，但不支持 video，video 会静默变 unknown。
- Worker 语音匹配只在 `msg_type == voice` 时执行；如果输入是 audio，Worker 可同时上报 audio 原消息和 voice 转写结果，后端再把 audio 变 voice，形成重复 voice。

应在 OmniAuto -> Worker 边界一次性把 audio 归一为 voice；后续不再重新解释。video 要么正式支持，要么显式拒绝，不能静默降级。

### P1-3 目标身份校验可以被省略

后端 ingest schema 中 `remark_code` 是可选字段；服务只在它存在时校验与绑定是否一致。Worker 当前会发送，但协议允许其他调用方省略后绕过新加的短码目标校验。

C2 ingest 应要求 `remark_code` 必填，并与 conversation binding 严格一致。

### P1-4 OCR 文本跨轮去重依赖可漂移的 occurrence index

Worker 对 OCR 文本使用 `role + type + content hash + occurrence_index`。它虽然计算了前后文，但前后文没有进入最终 key。相同文本中前一条滚出屏幕后，后一条 occurrence index 会从 1 变 0，可能与历史记录碰撞或产生新键。

### P1-5 raw_payload 承载了本应强类型化的关键协议

语音状态、anchor、canonical identity、判定证据、契约版本都在 raw JSON 里。Pydantic 和数据库无法校验字段缺失、枚举错误、外层与 envelope 冲突，也无法做可靠迁移。

### P1-6 现有测试覆盖了分支，却没有覆盖三层不变量

现有测试会分别验证：文本不因内容相同被提升为 voice、相同转写不同 anchor 可并存、partial 成功项可上报。但缺少以下关键契约测试：

- 同一 source/canonical identity 出现 `text/self` 与 `voice/customer` 时只能输出一条。
- 同一来源的 audio 与 voice transcription 只能输出一条 voice。
- 外层字段与 `message_envelope` 冲突时必须拒绝或按唯一权威修正。
- 同一来源不同 dedupe key 到达后端时必须冲突拒绝。
- 已展开语音的“头像 + 语音行 + 无头像转写行”跨轮仍识别为一个 voice。
- 页面大幅位移后身份稳定；两条相同内容/相同时长的真实消息仍能区分。
- `remark_code` 缺失必须拒绝。
- flow partial 与 item completed 的组合不会把转写行再次作为 text 上报。

## 四、统一方案：每层只做一件事

### 1. OmniAuto：只产出观察事实和结构关系

OmniAuto 负责：

- OCR 行、头像同排关系、左右原始 lane、语音时长/图标证据；
- 语音行与下方转写行的 parent-child 关系；
- 每个点击锚点的 item 状态和证据；
- 当前截图内的 observation identity。

OmniAuto 不负责最终跨轮 dedupe，也不让纯几何猜测覆盖同排头像或 voice parent 的角色。

### 2. Worker：成为唯一语义归一化裁判

Worker 负责把多路 observation 合并成唯一 `CanonicalMessageV2`：

```text
source_message_key
conversation_id / remark_code
sender_role: customer | self | system | unknown
message_type: text | image | voice | file | system | unknown
content
item_state: completed | failed
flow_state: completed | partial | failed | cancelled
evidence
dedupe_key
```

权威顺序必须写死：

```text
语音 anchor 的同排头像角色
> 普通消息的同排头像角色
> 明确 lane 结构
> 几何猜测
```

同一个 `source_message_key` 只能输出一条；voice 的结构化证据优先于同源 text 解释。若证据冲突且无法决定，宁可标记 conflict 并不上报，也不能两条都上报。

### 3. 后端：只校验和存储，不再重新分类

后端负责：

- 校验 schema/contract version、remark_code、枚举和必填证据；
- 校验同一 payload 内 source key 不重复且不冲突；
- 按 Worker 已归一化结果存储，不再自己重算 voice 类型或角色；
- 用两道唯一约束防守：

```text
unique(conversation_id, dedupe_key)                 # 跨轮幂等
unique(conversation_id, read_run_id, source_message_key) # 同轮同源唯一
```

对同一 source key 的不同 type/role/content 应返回明确 `MESSAGE_SOURCE_CONFLICT`，不得静默接收。

## 五、身份字段应拆成两类，不能再让一个 ID 包打天下

### observation_id

- 表示“某次截图中的某个 OCR/视觉观察”。
- 可以包含 capture/sidecar run 和局部行信息。
- 用于审计和同次操作关联，不承担跨轮去重。

### source_message_key / dedupe_key

- `source_message_key`：同一 read_run 内，同一视觉消息在 messages/voice 两条支路中的共同来源身份；不得包含最终推断的 role/type。
- `dedupe_key`：Worker 在完成归一化后生成的跨轮幂等键。
- 对无法稳定区分的重复消息，必须保守进入 conflict/人工证据，而不是用绝对坐标假装稳定。

## 六、建议实施顺序

1. **先写契约与失败测试，不改点击流程**：把上述 8 类缺失测试做成三层 contract matrix，并加入本轮真实 payload 回放。
2. **OmniAuto 修结构输出**：语音行与转写行先按 parent-child 结构合并，再继承 anchor 角色；输出 observation/source key 和 per-item 状态。
3. **Worker 建唯一 canonicalizer**：先建 source index，再做 voice/text 合并，最后统一生成一条消息和一个 dedupe key；删除独立支路直接 append 的可能。
4. **后端增加 V2 schema 和冲突门禁**：先兼容读、严格写；新增 source key 字段与唯一约束，remark_code 必填。
5. **迁移与数据清理**：先报告现有 6 组冲突，由业务确认保留正确 voice 记录；迁移不能自动按“后写覆盖前写”。
6. **回放测试后再做 Windows 实机**：固定截图/JSON 回放全部通过，再做少量真实微信验证，不再靠实机发现基础业务逻辑错误。

## 七、验收门槛

在进入下一次 Windows C2 回归前，至少应满足：

- 同一 source identity 在 Worker 输出中最多一条。
- 同一 source identity 的 type/role 冲突被自动化测试覆盖。
- 后端能拒绝 source conflict，即使 dedupe key 不同。
- 已展开 customer/self 语音结构回放均稳定为一条 voice。
- partial 只上传 completed items，不产生转写 text 副本。
- absolute x/y 变化不改变跨轮 dedupe；相同内容的两个真实气泡仍可区分。
- audio/voice、contact/customer、sales/self 只在一个边界完成归一化。
- remark_code 缺失或不一致均拒绝入库。
- 固定总时长不再中断有持续进展的 flow；无进展 watchdog 有独立测试。

## 八、审计结论

下一步不应继续对“某条语音被点两次”“某段文字被认成 self”分别打补丁。应先建立 `CanonicalMessageV2` 和 source identity 双重约束，让 OmniAuto 只报事实、Worker 唯一定性、后端只校验存储。只有把裁判收敛到一处，才会停止“新现象 -> 修现象 -> 另一层又重新解释”的循环。
