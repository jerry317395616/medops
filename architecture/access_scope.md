# 访问范围与数据权限查询

## 目标

COD-024 在 `medops` 内提供统一、默认拒绝的数据范围解析能力。业务模块不再自行拼接用户、
法人、医院和科室条件；所有组织范围查询复用同一受控契约，不修改 Frappe、ERPNext 或 Frappe HR。

## 数据模型

`HRP Access Scope` 定义一个法人、医院或组织单元范围。组织单元范围可以包含同一已发布组织版本的
全部启用下级节点。范围支持启停、有效期、修订号和策略摘要。

`HRP Access Scope Member` 是子表，一行绑定一个用户、可选目标 DocType 和六类操作：读取、新增、
修改、提交、撤销、导出。目标 DocType 留空代表所有能够安全映射该组织层级的受治理 DocType。
同一范围内不允许重复的“用户 + 目标 DocType”授权。

## 解析语义

`CORE-001` 是只读 POST API。调用方提交 `user`、`doctype` 和 `action`；普通用户只能解析自己，
`System Manager` 与 `HRP System Manager` 可以代查。解析器先验证 Frappe 基础 DocPerm，再计算当前
有效的访问范围：

- Administrator 和两个系统管理角色在基础权限允许时为全局范围；
- 其他用户没有匹配范围时默认拒绝；
- 多条范围按 OR 合并，每条范围内部的法人、医院、组织单元按 AND 组合；
- 组织下级只从同一已发布版本的 Nested Set 边界展开；
- 目标 DocType 缺少所需组织字段时丢弃该范围，最终无可用范围即拒绝；
- 输出为结构化字段、操作符和值，不接受配置 SQL、Python、表达式或任意字段名。

返回包含 `allowed`、`unrestricted`、`base_permission`、`scopes`、`filters`、`masking` 和
`decision_digest`。`masking` 在本版本明确为 `none`；字段级脱敏由后续独立策略扩展，不能借解析器
临时绕过。

## 查询接入

`permission_query_conditions` 和 `has_permission` 首先接入医院、组织版本、组织单元和标准组织映射。
列表查询生成仅含受控字段和值转义的 SQL 条件；单条读取使用同一结构化条件匹配。后续业务 DocType
必须具备明确的 `company`、`hospital`、`organization_unit` 字段映射并新增回归测试后才能接入。

## 审计与隐私

每次解析记录目标 DocType、操作、是否允许、是否全局、范围数量和决策摘要。不记录用户名、组织名称、
范围原值、HTTP 请求体或过滤值。解析为只读操作，不创建幂等记录，也不提交数据库事务。

## 迁移与回滚

`bench migrate` 创建两张表和有效期、组织、用户查询索引，并强制同步 `medops` 所拥有的四个标准
Workspace，修复运行站点中较新旧副本阻止源码入口更新的问题。升级前必须备份站点数据库。

回滚应用代码时保留访问范围及授权明细，不执行破坏性删除。旧版本不会读取新表；重新升级后可继续
使用原策略。若需紧急停止某项授权，先停用对应范围，再回滚代码。
