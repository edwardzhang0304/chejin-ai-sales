# 车金运营后台 v0.10.1

产品版本与 backend/pyproject.toml、Worker __version__ 和 contracts/c2_contract_v3.json 的 contract_revision 一致。Vite 从该合同注入页面可见版本，不单独维护页面版本常量。Hook 产品版本写在 contracts/hook_contract_v1.json 的 product_version；hook-v1 / C2 3 是协议结构号，不能改成产品号。

联调查看构建：VITE_API_BASE_URL=/api VITE_OPS_READ_ONLY=1 npm run build。只读标记用于页面提示和省略 Worker Token 请求；服务端的真实鉴权、路由许可和数据库写入限制由 ops/hook_uat/read_only_admin.py 实施，不能依赖前端参数作为权限门禁。正式运营构建不设置只读标记，原有业务功能不变。

该联调入口连接当前 r3 数据，不能代表生产部署或 Windows 实机验收已经完成。详细业务边界见 deliverables/AI智能客服售前跟进系统_PRD_运营后台统一版_v0.10.1.md。
