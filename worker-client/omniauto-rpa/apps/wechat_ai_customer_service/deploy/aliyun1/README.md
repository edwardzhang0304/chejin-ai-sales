# 阿里云1加密运行配置

本目录保存阿里云1 VPS admin 的加密运行配置快照。

- `vps_admin_control_plane.enc.json`：加密后的 VPS admin `control_plane.json`
- 目标路径：`/opt/omniauto-runtime/vps_admin/control_plane.json`
- 加密方式：AES-256-GCM
- KDF：PBKDF2-HMAC-SHA256
- 解密口令：使用本机 SSH 密钥仓库中的 `D:\AI\VPS_SSH_KEY-master\口令.txt`

快照包含 admin 初始化状态、绑定邮箱、SMTP 发信配置、客户账号与测试账号绑定邮箱等运行配置。快照不包含一次性验证码 challenge、当前登录 session、待执行命令队列。

不要提交明文 `control_plane.json`、SMTP 授权码、未加密的账号状态文件或临时解密产物。
