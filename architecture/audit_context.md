# 审计上下文与关联 ID

## 权威模型

`medops.common.audit_context.AuditContext` 是请求和后台任务上下文的唯一模型。
它是不可变的进程内对象，不是 DocType，也不产生数据库记录：

| 字段 | 来源 | 语义 |
| --- | --- | --- |
| `schema_version` | 应用常量 | 当前固定为 1 |
| `correlation_id` | 合法请求参数、请求头、父任务或服务端生成 | 关联同一业务调用的多个执行 |
| `request_id` | 服务端生成 | 唯一标识一次 HTTP、任务或服务执行 |
| `parent_request_id` | 受控任务载荷 | 标识创建当前后台任务的上游执行 |
| `channel` | 服务端 | `http`、`job` 或 `service` |

ID 只允许 ASCII 字母、数字、点、下划线、冒号、斜杠和连字符，以字母或数字
开头，最长 140 个字符。输入不做静默截断或空白修剪。HTTP 参数
`correlation_id` 保持对 `X-Correlation-ID` 的历史优先级；上下文建立后不能在
同一执行中替换为另一个 ID。`X-Request-ID` 输入不会被信任。

## HTTP 生命周期

锁定的 Frappe v17 先完成 `make_form_dict`，再调用 `before_request`，最后在
`process_response` 中合并 `frappe.local.response_headers`。应用据此注册：

```text
before_request -> start_http_audit_context
endpoint/error -> ensure_audit_context + emit_audit_event
after_request  -> finish_http_audit_context
response       -> X-Correlation-ID + X-Request-ID
```

非法调用方关联 ID 会先安装一个安全的服务端上下文，再通过
`IONE-CORE-0003` 返回 400；非法原值不会进入响应或日志。统一错误体新增
`correlation_id` 和 `request_id`，`error_id` 仍只标识单次错误。

## 直接服务调用

不经过 HTTP 或后台任务钩子的公开服务入口必须使用
`medops.services.audit_context.service_audit_scope`。每个最外层服务调用获得
独立的 `request_id`，退出时自动清理上下文；同一作用域内的嵌套服务复用上下文，
且不能替换 `correlation_id`。若已经存在 HTTP 或任务上下文，服务作用域只校验并
继承该上下文，不会提前清理或覆盖它。

这一区分避免 Bench 测试、控制台脚本和安装钩子在同一 Python 进程内顺序调用时
复用上一项工作的上下文，同时仍保证单次真实执行中的上下文不可变。

## 后台任务传播

跨队列调用必须使用 `medops.services.audit_context.enqueue_with_audit`。该服务
附加只包含 schema、关联 ID 和父请求 ID 的保留载荷。Frappe 的 `before_job`
钩子在调用目标函数前 `pop` 载荷并生成新的请求 ID，因此业务函数不会收到保留
参数。没有载荷的计划任务获得新的关联 ID；损坏载荷被丢弃、记录脱敏警告并以
安全上下文继续执行。

直接 `frappe.enqueue` 仍可运行，但不会继承上游关联 ID。领域服务需要跨模块或
跨队列传播时，应调用公开的 `enqueue_with_audit`，不得导入其他模块私有实现。

## 审计日志

自研应用结构化审计统一调用 `emit_audit_event`。服务自动添加上下文，只允许
命名空间内日志名、受控事件名和短标量字段。以下字段标记禁止进入应用审计：

- 用户、邮箱、患者和站点；
- 请求消息、正文和嵌套 payload；
- 密码、令牌和 secret；
- 文件或 Bench 路径、SQL。

运行时故障仍可使用 Frappe 标准 `frappe.log_error` 机制；业务审计不得绕过统一
服务直接调用 `frappe.logger`。

## API、权限与幂等

`GET /api/method/medops.api.v1.audit.get_audit_context` 只允许已认证用户，返回
当前安全上下文和 `http_write_enabled=false`。重复请求不写 `Version`、
`Comment` 或 `Error Log`；复用相同关联 ID 时，每个请求仍生成唯一请求 ID。

本能力不提供 HTTP 写接口，不新增角色、DocType、Fixture、Patch 或 Workspace，
不改变 ERPNext 和 Frappe HR 权限，也不执行会计、库存或 HR 业务过账。

## 工程脚本

`change_manager.py`、`environment_manager.py` 和 `fixture_manager.py` 共用纯 Python
标识符校验器。仓库契约阻止脚本或运行时代码重新定义关联 ID 正则、在统一服务
外读取 `X-Correlation-ID`，以及绕过脱敏服务写应用审计日志。


实际审计落盘复核：1627858在框架默认ERROR级别下，emit_audit_event正常返回但INFO未写入真实文件（0字节），已保存独立探针。统一审计入口现在只对经过medops命名校验的审计logger显式设置INFO，保留敏感字段前置拒绝，不调整框架/全局/其他应用级别，不补造历史日志。新增真实FileHandler写入测试，验证关联ID、请求ID、事件及全局/框架级别保持；库存453/446。新代码本地与原生复验待完成，历史主账/授权事实不修改。

86ba9b0的456项本地质量和6项定向原生通过，包含真实FileHandler验证；完整446中的2个失败属于其他HTTP幂等测试身份，审计用例无失败标记。命名INFO业务事件落盘已形成原生证据，保留原生/全局级别及敏感字段前置拒绝；全量修复、发布和真实医院验收仍分别完成。
