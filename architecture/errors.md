# I-ONE 统一异常与错误码

## 目标和边界

`medops/config/error_catalog.json` 是应用错误码、类别、HTTP 状态、公开消息、
重试语义和日志级别的唯一机器权威。错误码属于代码/API 契约，不使用可在生产
Site 修改的 DocType。所有模块通过 `medops.services.errors` 公共门面抛出
受控错误，禁止直接调用 `frappe.throw` 或 `frappe.only_for`。

该决策由
`architecture/adr/ADR-0003-source-controlled-error-contract.md` 记录。

## 命名和兼容

- 代码格式为 `IONE-CORE-xxxx`，从 `0001` 连续追加。
- 已发布 code 不删除、不改义、不复用；语义变化新增 code。
- symbolic key 只供 Python 调用，客户端只依赖稳定 code。
- 类别与 HTTP 状态由解析器固定映射，重试只允许 429、502、503、504。
- 公开英文消息必须是无占位符、无 HTML 的静态句子，中文使用
  `medops/translations/zh.csv`。

当前基础类别为认证、授权、校验、未找到、冲突、策略限制、依赖故障、限流和
内部错误。业务模块后续需要新增错误时，必须在目录末尾追加并同步翻译、API
契约、测试和任务变更记录。

## Frappe 响应契约

受控错误保留 Frappe v17 标准错误响应，同时增加：

```json
{
  "ione_error": {
    "schema_version": 1,
    "code": "IONE-CORE-0003",
    "category": "validation",
    "message": "请求参数无效。",
    "error_id": "random-support-reference",
    "retryable": false
  }
}
```

响应头包含 `X-Ione-Error-Code` 和 `X-Ione-Error-ID`。HTTP 状态取自目录，例如
认证 401、授权 403、校验 400、未找到 404、冲突 409、限流 429、依赖不可用
503、内部错误 500。成功响应不包含这两个头。

## 安全与审计

公开错误不返回 symbolic key、日志级别、内部异常消息、堆栈、文件路径、SQL、
请求参数、患者标识或凭据。应用审计只记录：

- `event=ione_error_raised`
- `error_id`
- code、category、HTTP 状态和 retryable
- 可选的 Python 原因类型名称，不记录原因文本

500 错误继续服从 Frappe 的标准服务端错误日志和 traceback 环境策略。`error_id`
仅用于定位单次失败，不承担跨请求关联；关联上下文由 COD-010 统一实现。

## 权限和写入

`GET /api/method/medops.api.v1.errors.get_error_catalog` 只允许
`System Manager` 或 `HRP System Manager`，返回可公开的稳定目录、数量与
确定性 SHA。不存在 HTTP 新增、修改、删除或重编号接口。目录查询只读，不创建
Version、Comment、Error Log 或业务记录。

该方法允许 Guest 进入应用鉴权层，仅用于让未登录请求也获得相同的
`IONE-CORE-0001`、401 状态和支持引用；Guest 在角色校验前不会读取目录，
目录内容仍只向上述管理角色返回。

## 开发用法

```python
from medops.services.errors import raise_ione_error

if invalid_input:
    raise_ione_error("INVALID_REQUEST")

try:
    load_internal_configuration()
except ConfigurationError as exc:
    raise_ione_error("CONFIGURATION_INVALID", cause=exc)
```

不得把用户输入拼入公开消息。`cause` 只允许审计原因类型，不建立会暴露原始
消息的异常链，消息内容不会进入应用结构化日志。未知 key 或损坏的目录会失败关闭为
`IONE-CORE-0012 INTERNAL_ERROR`。

## 验证、迁移和回滚

```bash
python scripts/repository_contract.py
python -m unittest tests.test_error_catalog
python scripts/quality.py
```

锁定 Bench 运行全部 `medops` 测试，覆盖真实 HTTP 状态、响应头、中文翻译、
角色权限、审计脱敏和只读幂等。本能力无数据库迁移。回滚时整体恢复旧调用与
目录，但已经对外发布的 code 不复用；生产 Site 无数据需要回退。
