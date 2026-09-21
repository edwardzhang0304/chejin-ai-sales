# 旧聊天相关入口清单（开发自审完成，待独立复审）

这份表是函数入口／用途清单，不是覆盖率证明，也没有把哈希、类型、权限等相等判断当作旧聊天正文比较。行号对应当前隔离源码。

| 源码位置 | 函数 | 处理 |
| --- | --- | --- |
| `worker-client/chejin_worker_client/task_runner.py:10044` | `_align_initial_identity_frame` | 当前入口已调用共享 HC；保持原媒体／身份检查。 |
| `worker-client/chejin_worker_client/task_runner.py:996` | `_payload_with_confirmed_text_candidate_receipts` | 有附加触发条件的全屏文字计数核对项；可能在共享连续性判断之前拒绝。按最新指示保留、不扩大修复，非普通聊天必然故障。 |
| `worker-client/chejin_worker_client/task_runner.py:1957` | `_image_flow_action_slot_continuity` | 当前入口已调用共享 HC；保持原媒体／身份检查。 |
| `worker-client/chejin_worker_client/task_runner.py:2094` | `_image_action_frame_to_reread_continuity` | 当前入口已调用共享 HC；保持原媒体／身份检查。 |
| `worker-client/chejin_worker_client/task_runner.py:21331` | `_converge_current_screen_after_images` | 当前入口已调用共享 HC；保持原媒体／身份检查。 |
| `worker-client/chejin_worker_client/task_runner.py:19728` | `_finish_new_visible_voices_in_current_chat` | 本轮补提前的准备帧变化判断；后续动作前后比较已有共享 HC。 |
| `worker-client/chejin_worker_client/pre_send_checkpoint.py:438` | `_compare_checkpoint_business_continuity_v5` | 精确连续性失败可进入共享 HC；已接受的 HC 不再被正文二次否决。 |
| `worker-client/chejin_worker_client/pre_send_checkpoint.py:647` | `compare_checkpoint_to_observations` | 精确连续性失败可进入共享 HC；已接受的 HC 不再被正文二次否决。 |
| `worker-client/chejin_worker_client/historical_alignment.py:6` | `checkpoint_for_target` | 共享历史权威、投影和帧比较入口；新读屏使用本轮共享规则。 |
| `worker-client/chejin_worker_client/historical_alignment.py:26` | `projected_frame` | 共享历史权威、投影和帧比较入口；新读屏使用本轮共享规则。 |
| `worker-client/chejin_worker_client/historical_alignment.py:72` | `reconcile_viewports` | 共享历史权威、投影和帧比较入口；新读屏使用本轮共享规则。 |
| `worker-client/chejin_worker_client/historical_alignment.py:119` | `bind_final_frame_correspondence` | 共享历史权威、投影和帧比较入口；新读屏使用本轮共享规则。 |
| `worker-client/chejin_worker_client/historical_alignment.py:31` | `admit_current_context_frame` | 共享历史权威、投影和帧比较入口；新读屏使用本轮共享规则。 |
| `worker-client/chejin_worker_client/historical_alignment.py:93` | `refreshed_correspondence` | 本轮补冻结回执兼容，完整重算凭证，只刷新权威摘要。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py:17587` | `validate_send_context_guard` | 输入前、输入后共用的守卫；旧聊天已有 HC，原截图／同帧摘要检查保留。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py:18037` | `build_send_fact_snapshot_from_frame` | 本轮补发送确认历史依据传递；匹配实际新发气泡仍用原发送校验。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py:18398` | `confirm_reply_sent` | 本轮补发送确认历史依据传递；匹配实际新发气泡仍用原发送校验。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py:10649` | `send_payload` | 本轮补发送确认历史依据传递；匹配实际新发气泡仍用原发送校验。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/text_correspondence.py:378` | `_historical_send_overlap` | 本轮统一旧前缀；Worker／后端共同使用发送后客户新消息判断。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/text_correspondence.py:416` | `find_new_matching_self_message` | 本轮统一旧前缀；Worker／后端共同使用发送后客户新消息判断。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/text_correspondence.py:464` | `confirmed_post_send_customer_suffix` | 本轮统一旧前缀；Worker／后端共同使用发送后客户新消息判断。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/historical_text_alignment.py:128` | `build_correspondence` | 同一 HC 实现；完成态旧语音纳入，冻结旧证明独立兼容解码。系统旧文字已同步后端重算及冲突入口。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/historical_text_alignment.py:399` | `_build_confidence_correspondence` | 同一 HC 实现；完成态旧语音纳入，冻结旧证明独立兼容解码。系统旧文字已同步后端重算及冲突入口。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/historical_text_alignment.py:215` | `comparison_projection` | 同一 HC 实现；完成态旧语音纳入，冻结旧证明独立兼容解码。系统旧文字已同步后端重算及冲突入口。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/historical_text_alignment.py:247` | `compare_historical_viewports` | 同一 HC 实现；完成态旧语音纳入，冻结旧证明独立兼容解码。系统旧文字已同步后端重算及冲突入口。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/historical_text_alignment.py:317` | `verify_correspondence` | 同一 HC 实现；完成态旧语音纳入，冻结旧证明独立兼容解码。系统旧文字已同步后端重算及冲突入口。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/send_interruption.py:15` | `_corresponding_sequences` | 输入中插话重算历史判断；本轮补旧两帧证明兼容，保留完整凭证相等校验。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/send_interruption.py:84` | `customer_interruption_proof` | 输入中插话重算历史判断；本轮补旧两帧证明兼容，保留完整凭证相等校验。 |
| `backend/app/services/historical_text_alignment.py:8` | `verified_pairs` | 权威数据库独立重算共享证明；不修改已保存事实。 |
| `backend/app/services/wechat_service.py:3709` | `_validate_non_delivered_frame_observations` | 已验证 HC 对应可解释旧正文差异；角色、来源、媒体和 AI 回执保护仍保留。 |
| `backend/app/services/wechat_service.py:3246` | `_verified_ai_reply_action_for_self_message` | 已验证 HC 对应可解释旧正文差异；角色、来源、媒体和 AI 回执保护仍保留。 |
| `backend/app/services/wechat_service.py:702` | `_raise_message_identity_collision` | text/voice/system 消费同一已验证历史对应；正常查询和 PostgreSQL 唯一冲突重查均传入完整对应结果。 |
| `backend/app/services/post_send_customer_read.py:8` | `settle` | 共享发送后判断只触发重新读取，不直接入库观察消息；已发送前缀依据原回执。 |
| `backend/app/services/post_send_customer_read.py:52` | `attach_confirmed_prefix` | 共享发送后判断只触发重新读取，不直接入库观察消息；已发送前缀依据原回执。 |
| `worker-client/chejin_worker_client/task_runner.py:9482` | `_expand_pre_send_continuity_context_once` | 车金 HC 当前屏补读已改用共享历史判断；保持单帧零滚动，后续最新帧与媒体校验照旧。 |
| `worker-client/chejin_worker_client/task_runner.py:9639` | `_expand_media_continuity_context_once` | 车金 HC 当前屏补读已改用共享历史判断；保持单帧零滚动，后续最新帧与媒体校验照旧。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py:8750` | `capture_message_history_snapshots_until_anchor` | 车金 HC 不再调用旧锚点搜索；实际 targeted 路由原本即强制零滚动/一帧。多帧合并属于独立产品兼容；单帧 occurrence 拼装保留重复消息。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py:8894` | `merge_message_history_snapshots` | 车金 HC 不再调用旧锚点搜索；实际 targeted 路由原本即强制零滚动/一帧。多帧合并属于独立产品兼容；单帧 occurrence 拼装保留重复消息。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py:10473` | `message_anchor_match_type` | 车金 HC 不再调用旧锚点搜索；实际 targeted 路由原本即强制零滚动/一帧。多帧合并属于独立产品兼容；单帧 occurrence 拼装保留重复消息。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/wechat_win32_ocr_sidecar.py:8981` | `sidecar_new_message_occurrences` | 本轮扫描未发现生产调用；不作为车金已接线入口，保持不动。 |
| `worker-client/chejin_worker_client/historical_correction_recovery.py:31` | `prepare` | 用户确认的例外：实际修正历史正文，保留原图修改范围和邻居校验。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/historical_text_correction.py:9` | `original_omission_candidate` | 用户确认的例外：原图事实修正，不以 HC 替代修改正文所需的证明。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/historical_text_correction.py:122` | `build_original_image_proposal` | 用户确认的例外：原图事实修正，不以 HC 替代修改正文所需的证明。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/adapters/wechat_connector.py:2103` | `verify_send_from_messages` | 比对本次新发送正文；按用户确认保留，不能用旧聊天相似度替代。 |
| `worker-client/omniauto-rpa/apps/wechat_ai_customer_service/admin_backend/services/raw_message_store.py:525` | `find_ocr_near_duplicate` | 用户明确确认排除：独立产品原始消息记录，不走车金身份链。 |
| `worker-client/chejin_worker_client/task_runner.py:19790` | `reconcile_prepare_frame` | 调用共享 HC 后才进入原身份/回执结算；嵌套回调及恢复后的最新帧逐项追踪。 |
| `worker-client/chejin_worker_client/task_runner.py:21571` | `compare_final_text_frame` | 调用共享 HC 后才进入原身份/回执结算；嵌套回调及恢复后的最新帧逐项追踪。 |
| `worker-client/chejin_worker_client/task_runner.py:9767` | `_compare_pre_send_fact_checkpoint_frame` | 调用共享 HC 后才进入原身份/回执结算；嵌套回调及恢复后的最新帧逐项追踪。 |
| `worker-client/chejin_worker_client/task_runner.py:16737` | `_finalize_pending_image_action_after_reread` | 调用共享 HC 后才进入原身份/回执结算；嵌套回调及恢复后的最新帧逐项追踪。 |
| `worker-client/chejin_worker_client/text_recheck.py:11` | `differing_text_observation_ids` | 只选择低置信度时重新观察的位置，不接受旧消息身份；HC 已通过的路径不调用它。 |
| `backend/app/services/wechat_service.py:1568` | `_complete_authoritative_viewport_confirmed` | 同帧完整载荷真实性摘要，不是跨帧旧聊天正文比较；保持严格。 |
| `worker-client/chejin_worker_client/task_runner.py:18759` | `_wait_and_send_current_c3_batch_impl` | 仅在原动作未触发、当前授权与事实已核对后更新准备帧；删除旧整份序列相等门禁，保留原动作绑定和未知禁止重发。 |
| `worker-client/chejin_worker_client/action_journal.py:220` | `refresh_unattempted_send_journal` | 仅在原动作未触发、当前授权与事实已核对后更新准备帧；删除旧整份序列相等门禁，保留原动作绑定和未知禁止重发。 |

补充保留项：schema 和同帧 observations 一致性、发送 token/正文 hash、AI 回执、媒体动作和内容 hash、剪贴板序号、包文件 SHA、知识/车辆版本摘要，属于真实性或其他业务校验；没有改为相似度。

独立产品的 `customer_service_scheduler`、`session_monitor`、`raw_message_store` 和智能记录导出继续保持原行为。车金 HC 当前屏补读已绕开旧锚点比较。多页合并没有在车金启用；独立产品及无 HC 的旧合同调用保持原行为。
