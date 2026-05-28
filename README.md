# 易销客聊天记录导出

## 官网静态页

项目官网已放在 `website/` 目录，可直接打开 `website/index.html` 预览。
如需用本地服务查看：

```bash
cd website
python3 -m http.server 8765
```

然后访问 `http://localhost:8765/`。

这个目录里的 `yxk_chat_export.py` 会按下面流程导出当前账号有权限访问的聊天记录：

1. 用 `APP ID + appSecret` 或现成 token 鉴权。
2. 查询员工微信号，读取返回的 `csIdWechatId/wechatId`。
3. 查询每个员工的好友和群列表，读取返回的 `id/talker`。
4. 调用 `/yxk/frontend/common/wx/fgmquery` 分页拉取聊天记录。
5. 输出按会话拆分的文件，以及总表文件。

## 用 APP ID / appSecret 运行

```bash
export YXK_APP_ID='你的APP ID'
export YXK_APP_SECRET='你的appSecret'

python3 yxk_chat_export.py \
  --start '2000-01-01 00:00:00' \
  --end '2026-05-09 23:59:59' \
  --format both \
  --out-dir yxk_chat_export
```

## 用现成 token 运行

```bash
export YXK_TOKEN='你的token'

python3 yxk_chat_export.py \
  --start '2000-01-01 00:00:00' \
  --end '2026-05-09 23:59:59' \
  --format both \
  --out-dir yxk_chat_export
```

## 只导出指定员工微信 ID

```bash
python3 yxk_chat_export.py \
  --token "$YXK_TOKEN" \
  --wechat-id '员工微信ID' \
  --format both
```

## 输出文件

- `wechat_accounts.jsonl`：员工微信账号列表
- `talkers.jsonl`：好友/群列表
- `all_messages.jsonl`：全部聊天记录，完整保留原始 message 字段
- `all_messages.csv`：常用字段表格版，适合 Excel 打开
- `员工微信ID_friend_好友ID.*`：单个好友会话
- `员工微信ID_group_群ID.*`：单个群会话
- `summary.json`：本次导出汇总

如果接口返回空列表，先检查后台的 IP 白名单、应用权限、员工微信 ID、时间范围，以及 token 是否过期。
