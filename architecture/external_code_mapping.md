# 外部编码映射

## 目标与边界

`HRP External Code Mapping` 在外部系统编码与 ERPNext 标准主数据之间提供确定性的双向解析。
它属于 `medops` 的 `HRP Master Data` 模块，不拥有也不修改 `Department`、`Cost Center`、
`Item`、`Supplier` 或 `Warehouse`。这些标准对象仍由 ERPNext 控制器维护。

映射只接受 `HRP Master Data Domain` 静态策略中登记的目标类型。领域必须启用，且其策略版本和
摘要必须与当前代码一致。映射可以作用于医院全局，也可以进一步限定到一个已发布、有效的组织单元。

## 数据模型

一条映射保存以下业务身份：

- 主数据域、法人、医院和可选组织单元；
- 外部系统、外部编码和可选显示名称；
- 由主数据域决定的标准 DocType 与内部记录名；
- 启用状态、生效日期、失效日期和乐观并发修订号。

外部编码保留大小写和前导零。主数据域、医院和外部系统代码按公共契约标准化为大写。空组织单元
使用固定的全局作用域键 `*`。

数据库不直接对六个长文本字段建立超长联合唯一索引。服务和控制器从完整身份生成两个带版本前缀的
SHA-256 键：

- `source_key`：同一作用域、主数据域和外部系统下，外部编码只能映射一次；
- `target_key`：同一作用域、主数据域和外部系统下，内部记录只能被反向映射一次。

两个键均为隐藏、只读字段，并由数据库唯一约束兜底。业务响应不暴露身份键。

## 服务与事务

`UpsertExternalCodeMappingService` 是唯一写入口。它在一个 Frappe 外层事务中完成角色检查、幂等
预留、主数据域行锁、作用域检查、标准目标检查、双向唯一性检查和乐观修订保存。服务不调用
`frappe.db.commit`，失败时由公共领域服务回滚到保存点。

创建要求 `expected_revision=0`；更新要求映射名和正修订号。外部身份不可修改，内部目标、显示名称、
启停、生效区间和备注可以在修订号匹配时更新。无变化更新不增加修订号。

`ResolveExternalCodeMappingService` 按外部编码解析内部记录；
`ResolveInternalCodeMappingService` 按内部记录解析外部编码。两个查询都要求显式 `effective_on`，
只返回启用且处于有效期内的唯一映射，并再次验证医院、组织范围、领域策略和标准目标状态。

## API

- `CORE-004`：`GET resolve_external_code_mapping`，按外部编码入站解析；
- `CORE-021`：`POST upsert_external_code_mapping`，创建或修订映射，必须提供 `Idempotency-Key`；
- `CORE-022`：`GET resolve_internal_code_mapping`，按内部记录出站解析。

所有 API 禁止 Guest。写入角色为 `System Manager`、`HRP System Manager`、`HRP Data Steward`；
上述角色和 `HRP Integration User` 可以查询。Desk 元数据仅授予读取权限，不能绕过服务直接写入。

## 审计与安全

审计记录服务名、方向、启用状态、修订号、变化字段名以及源/目标身份摘要。日志不记录外部显示名称、
备注原文或身份组成字段的原文。幂等记录只保存请求指纹和加密响应快照。

动态 SQL 标识符仅来自代码内的静态主数据目标策略或 `source_key`/`target_key` 白名单；所有业务值
使用绑定参数。服务不写入 GL Entry、Stock Ledger Entry 或 Bin，也不修改任何已提交标准单据。

## 迁移与回滚

`bench migrate` 创建映射表、两个命名唯一约束和有效期索引。迁移可以重复运行，不创建或修改 ERPNext
标准主数据。升级前必须备份站点数据库。

回滚应用提交后再次执行迁移，保留映射表、身份键、约束和业务数据，不执行破坏性删除。旧代码不会消费
新表；恢复包含本功能的版本后，原有映射仍可继续使用。
