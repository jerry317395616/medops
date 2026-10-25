# Outbox / Inbox 事务消息基类

## 目标与边界

COD-013 为 `medops` 内部领域事件和集成消息建立统一的持久化、权限、幂等、并发、重试
与审计契约。它只提供公共模型和服务模板，不提前实现 COD-101～103 的外部系统、接口配置、
真实网络投递或通用入站 HTTP 写入口。

Outbox 解决“业务写入成功但事件丢失”的原子性问题：业务服务在自己的数据库事务中追加
Outbox，事务提交后才允许调度传输。Inbox 解决至少一次投递带来的重复消费：同一
`consumer + event_id` 只保留一条处理记录，消费者业务写入与 `Processed` 状态必须在
同一事务完成。

## 模型契约

具体 Outbox / Inbox DocType 继承共同的 23 个字段：

- 事件身份：`event_id`、`event_type`、`source`、`destination`；
- 事件时间与引用：`occurred_at`、`correlation_id`、`causation_id`、
  `reference_doctype`、`reference_name`；
- 载荷完整性：`payload_schema_version`、规范化 `payload_json`、`payload_hash`；
- 处理状态：`status`、`attempt_count`、`available_at`；
- 租约：`claim_owner`、只保存哈希的 `processing_token_hash`、`lease_expires_at`；
- 完成与结果：`completed_at`、规范化 `result_json`、`result_hash`；
- 失败摘要：`last_error_code`、`last_error_message`。

载荷和结果只接受 JSON 对象，按 UTF-8、排序键和紧凑分隔符确定性序列化，并保存
SHA-256。事件 ID、类型、端点、版本、引用和关联关系一经创建不得更改。具体 DocType
必须是非 Single、非 Child、非 Submittable，关闭重命名和 Track Changes；DocPerm 只授予
读、报表和选择权限。

基类不建立“全业务万能消息表”。COD-102 和 COD-103 将分别在 `HRP Integration` 模块
创建结构化 Inbox / Outbox DocType，并声明自己的角色和服务。当前测试只在一次性 Site
动态创建临时 DocType，结束后删除。

## Outbox 状态机

状态为 `Pending → Processing → Delivered`，失败后为
`Processing → Failed → Processing`；达到最大尝试次数进入 `Dead Letter`。

1. 发布服务在调用者事务中写入 `Pending`，不提交事务、不发网络请求。
2. 领取服务使用 `FOR UPDATE NOWAIT` 锁定记录，验证 `available_at`，生成一次性令牌，
   数据库只保存令牌哈希，并写入有限租约。
3. 领取服务的外层事务提交后，传输适配器才可调用外部系统。
4. 成功或失败服务使用令牌、行锁和状态机完成记录，旧令牌立即失效。
5. 崩溃导致的过期租约可被重新领取，因此网络层语义是至少一次，不承诺正好一次。

每个命令复用 COD-011 的持久化幂等、权限先行、savepoint 和加密响应快照。锁竞争快速
返回受控冲突。基类服务不调用 `frappe.db.commit()`；事务提交属于 Frappe 请求、后台任务
或后续传输编排器。

## Inbox 状态机与去重

Inbox 名称由 `SHA-256(consumer + NUL + event_id)` 确定，形成数据库级消费者范围去重。
状态为 `Processing → Processed`；失败可进入 `Failed → Processing`，达到最大尝试次数
进入 `Ignored`。

开始服务的返回值包含 `should_process`：

- 新事件返回 `true` 和一次性处理令牌；
- 已成功处理的重复事件返回 `false` 与原结果快照；
- 相同身份但载荷哈希或事件元数据不同，返回幂等冲突；
- 正在有效租约内处理的重复事件，返回并发冲突；
- 失败或租约过期记录可在限制内重新处理。

消费者必须在同一事务中执行“开始 Inbox → 业务写入 → 完成 Inbox”。若业务异常，外层
事务整体回滚；错误边界可在独立重试事务中登记失败。不得先提交 Inbox 再修改业务，也
不得在消费事务中调用外部系统。

## 权限、API 与审计

控制器通过私有 `ContextVar` 拒绝服务上下文外的插入、保存、`db_set`、删除、重命名和
取消。每个具体服务的角色必须与对应 MessageBox 定义一致，并在幂等预留前校验。

`PLT-016` 只读返回字段、状态和交付语义。平台不提供“指定任意 DocType 发布或消费消息”
的 HTTP API；业务模块必须声明自己的领域服务 facade。

审计只记录服务、消息类型、DocType、状态、尝试次数和受控错误码，不记录事件 ID、令牌、
原始幂等键、载荷、结果或错误正文。处理令牌只通过加密幂等响应返回，数据库只保存哈希。

## 测试、迁移与回滚

纯单元测试覆盖定义、规范化 JSON、哈希、确定性命名、令牌和错误摘要。锁定 Bench 测试
覆盖服务专用 CRUD、权限先行、持久化幂等、真实 MariaDB 行锁、租约、重试、死信、
消费者去重、载荷冲突和真实 HTTP 权限。

COD-013 不新增生产 DocType、表、Fixture、Workspace 或数据 Patch。发布只需常规备份与
`bench --site <site> migrate`。回滚应用提交即可；一旦后续具体消息表上线，回滚必须先
暂停发布和消费，再由对应 COD 保留或迁移未完成消息，不能删除队列数据。
