# 不可变台账基类

## 目标和边界

COD-012 为 `medops` 内部业务台账提供统一的追加、冲销、权限、审计、幂等和并发契约。
它不创建“万能台账” DocType，也不提前实现预算或科室库存业务。后续
`HRP Budget Ledger`、`HRP Department Stock Ledger` 等具体 DocType 仍归各自模块，
只继承公共控制器和服务。

该基类不适用于 ERPNext 的 `GL Entry`、`Stock Ledger Entry` 或 `Bin`。财务和库存业务
必须继续调用 ERPNext 标准控制器，不得借本基类直接写标准台账。

## 模型契约

`ImmutableLedgerDefinition` 是不可变定义，声明：

- 具体 Ledger DocType；
- 可执行写入服务的角色；
- 冲销时取相反数的字段；
- 冲销时交换的字段对，例如借方和贷方；
- 冲销可覆盖和必须覆盖的凭证上下文字段。

具体 DocType 必须包含公共字段：

`company`、`organization_unit`、`posting_date`、`posting_time`、`voucher_type`、
`voucher_no`、`reference_type`、`reference_name`、`quantity`、`debit`、`credit`、
`amount`、`currency`、`is_reversal`、`reversal_of`、`dimensions_json` 和
`source_hash`。

DocType 必须是非 Single、非 Child、非 Submittable，关闭重命名和 Track Changes。所有
DocPerm 只能授予读、报表和选择权限，不得授予创建、写入、删除、提交、取消、修订或导入。
`reversal_of` 必须链接自身，两个 Dynamic Link 必须分别由 `voucher_type` 和
`reference_type` 控制。

`dimensions_json` 只接受 JSON 对象并以确定性键序保存。可选的 `source_hash` 必须是
小写 SHA-256。业务敏感载荷不会写入平台审计日志。

## 控制器和服务

具体控制器继承 `ImmutableLedgerDocument`：

```python
class HRPBudgetLedger(ImmutableLedgerDocument):
	ledger_definition = BUDGET_LEDGER_DEFINITION
```

直接 CRUD 即使使用 `ignore_permissions=True` 也会失败。控制器拒绝：

- 未进入领域服务上下文的插入；
- `save`、`db_update` 和 `db_set`；
- 删除、重命名、取消和提交后更新。

具体追加和冲销命令分别继承 `AppendImmutableLedgerService` 和
`ReverseImmutableLedgerService`。二者复用 COD-011 的权限先行、savepoint、持久化幂等、
关联 ID、加密响应快照和受控异常。服务不调用 `frappe.db.commit()`，事务最终提交仍由
Frappe HTTP 请求或后台任务拥有。

## 冲销和并发

冲销服务按以下顺序执行：

1. 以 `FOR UPDATE NOWAIT` 锁定原记录；
2. 拒绝不存在、已是冲销记录或已有冲销记录的目标；
3. 复制原记录维度和业务字段；
4. 对定义中的字段取相反数，对字段对执行交换；
5. 写入 `is_reversal = 1` 和 `reversal_of`；
6. 通过同一控制器校验并追加新记录。

所有合法冲销都先锁定同一原记录，因此并发请求串行化。锁竞争快速返回
`IONE-CORE-0005`，不会长时间等待；第一笔成功后，后续请求返回
`IONE-CORE-0006`。同一幂等键重试则由领域服务重放原响应，不重复落账。

## API 和可观测性

`PLT-015` 只读返回公共字段、变更策略和冲销策略。平台不提供“指定任意 DocType 写台账”
的 HTTP API。每个业务域必须暴露自己的受权 facade，避免调用方绕过业务规则。

追加、冲销、开始、完成、重放和失败会写入脱敏审计事件。事件只包含服务、版本、Ledger
DocType、受控错误和关联信息，不包含金额、患者、员工、供应商、凭证正文或幂等原始键。

## 测试和发布

纯单元测试覆盖定义、JSON、字段白名单、等额反向和篡改。锁定 Bench 集成测试在测试 Site
动态创建临时 Ledger DocType，并覆盖权限、直接 CRUD、幂等、异常回滚、冲销、重复冲销、
行锁竞争和 HTTP 权限；测试结束后删除临时表和元数据，生产迁移不会新增通用台账表。

COD-012 本身没有业务数据迁移。发布只需常规备份和 `bench --site <site> migrate`。后续
具体台账 DocType 上线时必须单独提供迁移、回滚、保留期和对账方案。

## COD-033 精确金额扩展（开发中）

[ADR-0030](adr/ADR-0030-exact-budget-ledger-and-release-authority.md)增加显式金额版本2。
版本1、默认定义和公开只读契约保持原样。版本2以必填Data字段保存金额、借方和贷方，
同时保存必填整数金额版本及原始币种精度（0至9位）。服务先验证原始输入，拒绝二进制
浮点数、非有限值、溢出及超出原精度的金额，再建立Frappe文档，避免字段转换掩盖错误。
冲销使用原始精度并校验全部原业务维度，不读取当前币种默认值重算历史金额。

两个事务内追加/冲销辅助函数复用同一控制器、权限和脱敏审计，供拥有完整外层事务及
业务授权的批量领域服务调用。具体预算控制器仍必须验证其专属授权；辅助函数不提供
HTTP写入口，不提交事务，也不替代独立人工确认。旧事务快照下的冲销检查使用当前锁定
读取，避免漏读另一事务已经提交的冲销。真实双连接回归覆盖该情形。
若数据库快照隔离因记录变更抛出QueryDeadlockError，保留Frappe最外层事务重试信号；
不得转成尝试回滚已失效savepoint的普通业务错误。新事务重试仍必须拒绝已存在的冲销。

当前只有公共内核和隔离测试实现；具体预算模型、人工确认、任务执行、科室账簿及页面
仍在开发，不能据此宣称预算下达或完整HRP已经上线可用。
