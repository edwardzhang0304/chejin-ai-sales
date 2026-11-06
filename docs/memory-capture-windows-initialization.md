# Windows 内存采集目录初始化边界

本说明对应内存采集修复的第一轮复审补修。源码和本地替身验证不能替代 Windows 原生验收；不涉及业务状态、OCR 参数、绑定、发送记录或正式打包。

## 统一入口

`Coordinator.start → Channel.initialization → restrict → windows_security → Channel.publish`。
请求端仍调用同一个 `windows_security`，但只校验，不设置权限。`Channel.initialize` 供无需发布入口的初始化调用使用，同样进入上述上下文。

- Windows 初始化逐层打开目录，使用 `FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS`，只共享读，拒绝重解析目录；祖先和目标句柄保留到入口发布结束。
- owner/DACL 读取和修改使用句柄 `GetSecurityInfo / SetSecurityInfo`，不再通过可替换名称设置权限。额外检查目录身份；不依赖重复 `lstat` 作为互斥机制。
- 当前用户与 SYSTEM 的私有 ACL 可保留既有诊断记录；默认 owner 为 Administrators 时允许修正 owner。
- 空目录恢复进一步收窄：owner 必须是当前用户或等于当前 token 默认 owner 的 Administrators，ACL 只含当前用户、SYSTEM、Administrators 的允许项，当前用户有有效完全控制。任意其他账号或 Everyone 可访问时，即使目录为空也拒绝。
- 这不是防御当前用户、SYSTEM 或提升权限管理员的任意恶意行为的隔离层。目录共享模式也不能被表述成“锁住所有子文件”；防止普通外部账号写入依赖严格 ACL 条件。
- 允许的空结构仅为完全空目录，或 `memory-capture` 下只有空的真实 `requests` 目录；已有子目录独立校验并固定。设置权限后重新核对安全描述符、对象身份和空结构。
- 修正 ACL 不带继承 ACE，不向已有记录传播权限。失败仅禁用采集，不修改业务接单状态，也不删除记录。
- 恢复后的发布采用临时文件加原子、不覆盖的硬链接；最后检查之后出现同名入口也必须拒绝并保留。文件系统不支持该操作时拒绝，不降级为覆盖。

## 固定失败证据

统一 `ChannelSecurityError / _security_call` 保留固定 `phase`、固定 `stage`、数值 `windows_error_code` 和已取得的布尔事实。禁止保存原生异常原文、目录路径、账户名称或 SID。

操作阶段覆盖 token 获取/读取/关闭、主体识别、目录打开/读取/关闭、描述符读取、DACL 构造/设置、owner 设置、设置后重检、目录创建和入口发布。固定格式为 `channel_security_<stage>_failed`。明确判断拒绝仍使用 owner/permissions/bootstrap 等固定原因。

启动失败经 `install → memory-capture-gap.json → ZIP` 传递；请求失败经 `request_capture → memory/request.json → ZIP` 传递。原生调用成功但设置未生效，也必须在重检时拒绝。

## 验证与待验

本地覆盖：真实文件插入/目录替换配合 Windows API 句柄模型；真实采集及 ZIP 导出；失败阶段与数值错误码、不泄露敏感异常文本、原发送证据和业务状态不变。模型 fixture 从旧的 Everyone 可写空目录改为现场同构的管理员默认 ACL，并另加 Everyone/陌生账号空目录必须拒绝的反例；未删除原记录保留断言。

Windows 待验：默认 owner 与 User 不同的正常初始化、原生共享冲突、祖先目录更名、junction/reparse、ACL 继承不传播、重启保留历史记录，以及两进程真实 30 秒采集导出。现有原生测试在非 Windows 平台跳过，不计通过。

参考：
- https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
- https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-setsecurityinfo
