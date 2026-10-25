# 主数据质量规则

## 目标与边界

`HRP Data Quality Rule` 对 ERPNext 标准主数据执行可审计、可重复的质量检查，
`HRP Data Quality Issue` 保存检查失败形成的问题台账。两者属于 `medops` 的
`HRP Master Data` 模块，不拥有也不修改 `Department`、`Cost Center`、`Item`、
`Supplier` 或 `Warehouse`。

规则只能引用已启用的 `HRP Master Data Domain` 及其静态字段策略。运行时只读取标准记录；
任何标准主数据变更仍须经过其原生控制器或主数据变更申请流程。

## 规则模型

规则由稳定编码、主数据域、法人、医院、可选组织单元、目标字段、规则类型、规范参数、
严重程度和有效期组成。编码、主数据域和组织身份创建后不可改变；配置更新使用乐观修订号。

允许的规则类型只有：

- `Required`：值必须存在；
- `Allowed Values`：值必须属于最多 64 项的显式白名单；
- `Maximum Length`：文本长度不得超过 1 至 500；
- `Named Pattern`：只使用代码内固定的命名格式；
- `Reference Exists`：链接字段指向的记录必须存在。

命名格式限定为 `UPPER_CODE`、`EMAIL`、`CN_MOBILE`、`NUMERIC` 和
`ALPHANUMERIC`。规则不接受用户 SQL、Python、表达式或任意正则；参数 JSON 最大 8 KiB，
会在保存前规范化并生成 SHA-256 规则摘要。

## 问题生命周期

每个“规则 + 目标 DocType + 目标记录”确定性生成一个 `issue_key`，数据库唯一约束防止并发重复。
首次失败创建 `Open` 问题；后续失败更新评估时间并增加发现次数；通过后自动变为 `Resolved`；
再次失败时重开同一个问题。已解决且再次通过时不产生无意义修订。

问题单不是人工业务申请，因此不可提交、取消或删除。Desk 仅供读取；创建、解决和重开只允许领域
服务完成。问题响应不包含观察值摘要和规则摘要。

## 服务、任务与事务

`UpsertDataQualityRuleService` 是规则的唯一写入口，负责角色、幂等、组织范围、静态字段策略、
行锁和乐观修订。`EvaluateDataQualityService` 锁定规则修订和目标记录，在一个 Frappe 外层事务中
完成读取、评估及问题状态变更。两个服务都不调用 `frappe.db.commit`。

每日调度器只扫描当日有效规则，并为每条规则排入长队列。工作任务按目标记录名称稳定分页，
每批最多 200 条；每个“规则修订 + 目标 + 日期”具有确定性的幂等键。单条业务失败会被计数，
不会阻断同批其他记录；批次达到上限时再排下一批，避免单任务无界运行。

## API 与权限

- `CORE-023`：`POST upsert_data_quality_rule`，创建或修订规则；
- `CORE-024`：`POST evaluate_data_quality`，评估一条标准主数据并维护问题；
- `CORE-025`：`GET get_data_quality_issue`，读取脱敏问题。

两个 POST 都要求 `Idempotency-Key`。所有接口拒绝 Guest，仅 `System Manager`、
`HRP System Manager` 和 `HRP Data Steward` 可使用。DocType 元数据只授予这些角色读取权限，
不能绕过服务直接写入。

## 审计与安全

审计记录规则摘要、规则修订、问题唯一键、失败代码、问题修订和批次数量，不记录标准主数据观察值、
规则备注原文或规则参数原文。问题表只保存观察值的 SHA-256 摘要，业务响应不返回该摘要。

动态 SQL 标识符完全来自代码内的主数据静态策略；记录名和其他业务值均使用绑定参数。服务不写入
`GL Entry`、`Stock Ledger Entry` 或 `Bin`，也不修改标准主数据。

## 迁移与回滚

`bench migrate` 创建两张表、规则调度索引、问题唯一键和问题状态索引，且可重复运行。升级前必须备份
站点数据库。回滚应用提交后再次迁移，保留规则、问题、摘要和索引数据，不执行破坏性删除。

