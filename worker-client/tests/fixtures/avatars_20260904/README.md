# 2026-09-04 原始发送证据

来源：用户提供的 `chat_reply.zip / chat_reply/20260904_183802/`。
PNG 均按字节复制，未裁剪、缩放、涂改或合成。原图为 920×991。

| 文件 | SHA-256 |
|---|---|
| send_baseline_1788518283890.png | 27c7ab9a37feb9ea8f28d34351136dc07067ef3853a0bdc0447d9cb2ab4bcc1f |
| send_input_probe_1_1788518311798.png | c76620f08acafae0ec8d98a645ec04a68ea055cf4913f8a9327b685df3754f0d |
| send_post_guard_and_result_confirm_1_1788518325726.png | 6a6578d6d718ee72a526f8501ce2a212759f608c627660a3f0eb21e0c823d042 |
| send_result_confirm_2_1788518344534.png | 86de77c6023fe9b11514ca37a3b3cf9576ce70b3e7bdc64f9271a7c81bcb9199 |
| send_result_confirm_3_1788518359097.png | 86de77c6023fe9b11514ca37a3b3cf9576ce70b3e7bdc64f9271a7c81bcb9199 |
| send_result_confirm_4_1788518374902.png | 9d98a1b8ffeb5408b05615aa36ea631519ea6d2d798b431d7f18516eb48fb0ed |

四个发送后文件只有三份不同内容，第 2/3 次完全相同，不算四个独立场景。

`test_frame_avatars.py` 用真实 RapidOCR、生产启动标定、原图头像检测、
文字分组和原发送确认。布局由启动标定函数重建，Windows 几何/DPI 为受控输入，
不是恢复原 Windows 进程的内存状态。额外尺寸/DPI、留白头像和媒体边界图为
明确标注的程序化夹具，不是实机截图。

`backend/tests/test_avatar_send_closure.py` 使用原 S0、输入后 S1、发送后 S2，
从正式 `TaskRunner._execute_task()` 进入：自动登记 Flow、发送前复读、Bridge 参数 JSON、
Sidecar 正式发送事务、Worker 回执、真实本地 SQLite、正式后端 HTTP 路由与测试数据库、
UI 锁释放和 Flow 结束。测试不调用回执/解锁/结束 Flow 来代替生产收尾，还验证 HTTP
确认时锁仍持有、结束 Flow 时锁已释放且回执已结算。
S0 中已经完成转写的历史语音及已批准回复属于前置状态：历史正文来自原图真实 OCR，
此前语音动作的已提交注解为显式夹具，经正式 Worker 序列化/C2 HTTP 生成后端 checkpoint。
本测试不宣称重演之前的语音点击。Windows 进程传输、输入定位、键盘和焦点为受控边界。未替代 OCR、
头像检测、分组、连续性比较或发送确认，不代表真实 Windows 鼠标键盘 UAT，
也不宣称覆盖完整 C0–C4 调度主循环。

本目录为工程测试证据，不是第五份产品/架构权威文档。
