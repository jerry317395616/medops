# 领域服务与写入幂等

## 目标

`medops.services.domain_service.DomainService` 是自研业务命令的统一执行边界。它在调用
领域逻辑前完成权限、输入和幂等校验，在同一数据库事务内完成领域写入、响应快照和审计，
并把异常转换为稳定的 `IONE-CORE-xxxx` 错误。业务模块不得复制这套执行流程，也不得在
服务中直接提交事务。

纯 Python 契约位于 `medops.common.domain_service`，不依赖 Frappe，负责服务定义、
确定性 JSON、指纹、幂等键和执行结果模型。Frappe 模板方法位于
`medops.services.domain_service`，持久化实现位于
`medops.services.idempotency`。

## 服务定义

每个服务必须声明不可变的 `DomainServiceDefinition`：

- `name`：小写命名空间名称，例如 `hrp_foundation.module_setting.set_enabled`。
- `version`：服务请求语义发生变化时递增。
- `kind`：`command` 或 `query`。
- `required_roles`：至少一个允许执行该服务的角色。
- `idempotency_ttl_seconds`：命令结果可重放的有效期，默认 24 小时。

命令实现 `request_payload()` 和 `perform()`。`request_payload()` 只返回决定业务结果的
确定性 JSON 对象；不得放入关联 ID、请求 ID、幂等键、当前时间、用户、密码、令牌或文件
路径。`perform()` 返回 JSON 对象，不得调用 `frappe.db.commit()`，不得绕过 ERPNext 标准
控制器写入受保护台账。

业务模块只在自己的 `services` 包暴露公共 facade。API、任务和其他模块调用 facade，不
直接导入目标模块的 DocType 控制器。

## 执行顺序

命令按固定顺序执行：

1. 建立或继承审计上下文。
2. 校验角色。
3. 校验领域命令。
4. 规范化请求载荷并计算 SHA-256 指纹。
5. 从显式参数或 `Idempotency-Key` 请求头读取幂等键。
6. 建立数据库 savepoint，预留幂等记录。
7. 已完成且请求指纹相同则解密并重放响应，不再次执行领域写入。
8. 执行领域逻辑，规范化返回值，保存加密响应快照。
9. 返回本次关联 ID、请求 ID 和是否重放。

同一个服务和幂等键对应一个确定性 `idp-<sha256>` 记录名。相同键与不同请求指纹返回
`IONE-CORE-0007`；执行中的并发请求返回冲突；过期记录可由新请求替换。

## 事务与失败

领域服务只创建 savepoint，不拥有请求事务的最终提交。成功结果、业务写入和幂等记录由
Frappe 请求事务一起提交。任何受控或未知异常都回滚到该 savepoint，因此不会留下部分
业务写入或孤立的“执行中”记录。文件系统、对象存储和外部网络调用不受数据库 savepoint
保护；需要这些副作用的服务必须使用 outbox、后台任务或补偿流程，并另行记录 ADR。

每次服务调用使用独立 savepoint 标识，不能仅用共享请求 ID 命名。嵌套子服务成功后，
父服务失败仍必须撤销父子业务写入与幂等预留，同时保留调用父服务之前的事务写入。
调用方捕获失败后继续工作，不应提交失败服务留下的半笔数据。

未知异常只能记录异常类型，不能把异常消息、请求正文或业务载荷写入审计；对外统一返回
内部错误。领域校验、权限、资源不存在和幂等冲突使用错误目录中的稳定错误。

## 持久化与安全

`HRP Service Idempotency` 是 `HRP Foundation` 中的内部 DocType：

- 不保存原始幂等键，只保存 SHA-256。
- 不保存原始请求，只保存请求指纹。
- 响应先按确定性 JSON 规范化，再使用 Site 加密密钥加密。
- 保存响应指纹，重放前验证版本、解密结果、指纹和规范化形式。
- 完成记录不可修改；基础身份字段在执行中也不可修改。
- 仅 `System Manager` 和 `HRP System Manager` 可只读查询；应用服务使用
  `ignore_permissions=True` 写入，普通用户不能创建、修改或删除。
- DocType 不加入 Workspace、全局搜索或 Fixture。

密钥轮换前必须确认旧 Site 加密密钥仍可解密有效期内的快照。备份和恢复必须同时包含
数据库与 Site 私钥；不能只恢复其中一项。

### 私有诊断值

canonical_json_object保留规范JSON字节及基础标量协议，将规范化对象中的字符串值和完整序列化字符串包装为PrivateText。其encode保留原字节并返回PrivateBytes；repr及锁定Sentry SDK的__sentry_repr__只返回固定脱敏文本。正常JSON传输、指纹、数据库加密输入和比较保持原值，响应仍由当前角色、字段及范围授权约束。

生成的确认证明和消息租约nonce必须立即包装，幂等解密明文也必须先包装，再解析及返回。验权时保留证明的私有表示，避免str转换把明文重新留在SQL异常栈帧。应用不修改上游格式器或关闭日志；测试同时检查原生Error Log、遥测消息和实际SDK局部变量序列化，不发出遥测请求。该值边界不覆盖在应用钩子之前已进入原生请求解析/会话初始化的明文，早期入口须单独验证。

## API 契约

所有写 API 必须把 `Idempotency-Key` 请求头声明为必填，长度 8 至 140，只允许受控 ASCII
字符。为兼容受信任的内部调用，服务 facade 可显式传入幂等键，但 HTTP 调用以请求头为
正式契约。响应至少包含：

- 领域结果；
- `correlation_id`；
- 服务端生成的 `request_id`；
- `idempotency_replayed`。

重放只复用领域结果快照。新的 HTTP 请求仍获得新的请求 ID，并保留自己的审计上下文。

## 示例

`HRP Foundation` 的模块启停服务是首个实现：

```python
from medops.hrp_foundation.services import set_module_enabled

result = set_module_enabled(
    "HRP Budget",
    True,
    idempotency_key="deploy-20260729-budget-enable",
    correlation_id="release-20260729",
)
```

## 验证与退出条件

仓库测试验证纯契约、权限先于预留、成功与重放、同键异参冲突、缺失和非法键、加密快照、
未知异常回滚及 HTTP 请求头。锁定 Bench 集成测试必须在真实 MariaDB、Frappe 请求事务和
Site 加密密钥下运行。

若未来锁定 Frappe 版本提供等价且经过验证的标准领域服务和持久化幂等机制，可用新 ADR
替代本设计；迁移必须保留有效快照或等待其过期，不能静默删除正在生效的幂等记录。
