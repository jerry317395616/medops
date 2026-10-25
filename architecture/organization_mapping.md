# 组织与标准主数据映射

## 目标

`HRP Organization Mapping` 把 COD-018 发布的医院组织快照映射到 ERPNext 标准
`Department` 和 `Cost Center`。HRP 业务可以按组织版本保留历史口径，同时继续通过
ERPNext 标准控制器处理部门和成本中心相关业务。

映射是连接层，不拥有标准主数据。`medops` 不创建、重命名、移动、停用或删除
`Department`/`Cost Center`，这些操作仍由 ERPNext 标准控制器负责。

## 聚合与约束

每条映射以 `organization_version + organization_unit` 为业务身份，并复制法人、医院、
组织编码和组织类型用于确定查询及审计。记录至少包含一个标准部门或标准成本中心。

- 仅已发布组织版本可以建立或修改映射。
- 启用映射时，组织单元和标准目标都必须启用。
- 标准目标必须属于组织版本所在的同一 `Company`。
- 同一组织版本内，一个标准部门或成本中心只能映射到一个组织单元。
- 不同组织版本允许复用同一标准目标，以保留跨版本连续性。
- 已映射的组织祖先/后代关系必须与对应标准 Nested Set 祖先/后代关系一致。
- 映射记录只能由领域服务写入；Desk 权限为只读。

## 服务与 API

`UpsertOrganizationMappingService` 在单一数据库事务内依次锁定组织单元、现有映射和标准
目标，校验版本状态、法人、启停、目标唯一性和树关系，再以乐观 `revision` 保存。写入
必须提供 `Idempotency-Key`，同键异参会被拒绝。

`ResolveOrganizationMappingService` 支持两种确定性查询：

1. 直接按 `organization_unit` 查询。
2. 按 `hospital + unit_code + effective_on` 选择查询日最新发布版本。

查询会再次校验映射及标准目标仍然启用，避免向下游返回失效引用。

公开契约：

- `CORE-014`：创建或修订组织标准映射。
- `CORE-015`：按组织单元或业务日期解析组织标准映射。

## 权限与审计

`System Manager`、`HRP System Manager` 和 `HRP Data Steward` 可以维护映射。
`HRP Integration User` 仅可查询。角色检查发生在幂等预留之前。

审计只记录修订号、启用状态、目标数量和变化字段名。幂等指纹仅保存备注 SHA-256 摘要，
不保存部门名称、成本中心名称或备注原文。

## 迁移与回滚

安装迁移新增映射表和三个数据库唯一约束：版本与组织单元、版本与部门、版本与成本中心。
迁移可重复执行，不会创建或修改 ERPNext 标准记录。

升级前备份站点数据库。回滚应用提交并运行 `bench migrate` 时保留映射表和数据，不执行
破坏性删除。旧代码不会消费映射表；后续恢复新版本后数据仍可继续使用。
