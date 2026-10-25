# 主数据域与变更申请

## 目标与边界

COD-020 为 ERPNext 标准主数据建立可审计的“提案与复核”层，不拥有或替代标准记录。
当前支持 `Department`、`Cost Center`、`Item`、`Supplier` 和 `Warehouse`。批准申请
不会直接写标准 DocType；后续执行必须调用对应 ERPNext 标准控制器。

## 模型

`HRP Master Data Domain` 以域编码命名，一种标准 DocType 只能存在一个域。允许操作、
字段和值类型来自 `medops.common.master_data.MASTER_DATA_TARGET_POLICIES`，保存策略版本
和摘要用于发现配置漂移。

`HRP Master Data Request` 是可提交单据，字段提案保存在 `HRP Master Data Change Item`：

- `Create` 不允许目标名称，并要求策略中的新增必填字段。
- `Update` 要求目标名称，保存目标 `modified` 和每个字段当前值。
- `Disable` 只允许把 `disabled` 提议为真。
- 每个申请绑定法人、医院、已发布组织版本中的有效组织单元和期望生效日期。
- 一次最多 64 个字段，原始 JSON 不超过 64 KiB，字段名必须唯一。

## 状态与并发

状态为 `Draft -> Pending Review -> Approved/Rejected`。申请通过 `submit` 进入待审，
提交后不能修改提案、取消或删除。审核人不得是申请人。

所有命令都要求 `Idempotency-Key`。领域和申请使用 `revision` 乐观锁；组织单元、目标
主数据和申请行使用数据库行锁。保存、提交和批准都计算提案摘要；目标 `modified`、当前值、
组织有效期或策略摘要变化会产生冲突，不会静默覆盖。

## 服务与 API

- `CORE-016`：创建或修订主数据域。
- `CORE-017`：创建或修订申请草稿。
- `CORE-018`：提交草稿。
- `CORE-019`：批准或拒绝待审申请。
- `CORE-020`：按名称查询申请和字段提案。

DocType 在 Desk 中只读，服务负责所有写入。管理员可读取全部申请；普通 `HRP User` 只能
维护和读取自己的申请。

## 权限与审计

`System Manager`、`HRP System Manager` 和 `HRP Data Steward` 可维护域和审核申请。
上述角色及 `HRP User` 可创建申请。审核采用申请人/审核人职责分离。

审计不记录字段当前值、建议值、主题、原因或备注原文，只记录提案/策略摘要、操作、修订、
字段数量和字段名。幂等响应快照沿用应用级加密存储。

## 迁移与回滚

安装和迁移重复执行 `ensure_master_data_governance()`，建立目标类型唯一约束和申请查询
索引。迁移不创建或修改任何 ERPNext 标准主数据。

升级前备份数据库。回滚代码并运行 `bench migrate`，保留三个表及历史提案，不执行删除。
