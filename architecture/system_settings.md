# HRP System Settings

## 目标与边界

`HRP System Settings` 是 `medops` 内 `HRP Foundation` 模块拥有的标准 Single
DocType。它只保存全站通用且结构明确的安全、集成和默认组织配置，不承载业务主数据，
也不提供任意 JSON、动态代码或上游应用覆盖入口。

该能力不修改 Frappe、ERPNext 或 Frappe HR。默认法人复用 ERPNext `Company`；
COD-018 已将默认医院升级为 `HRP Hospital` Link，医院必须属于同一默认法人。升级迁移会把
旧版受控标识确定性转换为医院主数据，并保留原显示名称。

## 配置模型

| 字段 | 可编辑 | 约束 |
| --- | --- | --- |
| `enabled` | 是 | 严格布尔值 |
| `release_channel` | 否 | 固定 `locked-develop` |
| `configuration_version` | 否 | 从 1 开始的单调正整数 |
| `default_company` | 是 | 可空 `Company` Link |
| `default_hospital` | 是 | 可空 `HRP Hospital` Link；存在时必须属于默认法人 |
| `strict_data_scope` | 否 | 固定启用 |
| `require_human_confirmation_for_ai` | 否 | 固定启用 |
| `integration_timeout_seconds` | 是 | 5 至 300 秒，默认 30 |
| `remarks` | 是 | 可空，最多 500 字符 |

固定字段是代码发布政策，不是租户开关。即使绕过前端直接保存 DocType，控制器也会拒绝
弱化发布通道、严格数据域或 AI 人工确认。接口和持久化模型均没有
`configuration_json`。

## 服务与接口

公共服务位于 `medops.hrp_foundation.services.system_settings`：

- `get_system_settings()`：仅系统管理角色可读，返回确定性公开状态。
- `update_system_settings()`：通过 `DomainService` 执行，禁止直接提交事务。

HTTP 契约：

```text
GET  /api/method/medops.api.v1.settings.get_system_settings
POST /api/method/medops.api.v1.settings.update_system_settings
```

POST 必须携带 `Idempotency-Key`，并提交客户端最后读取到的 `expected_version`。
服务先执行角色和领域校验，再预留加密幂等记录；持久化前对 `tabSingles` 中的
`configuration_version` 执行 `SELECT ... FOR UPDATE`。版本不匹配返回
`IONE-CORE-0005`，同幂等键异参返回 `IONE-CORE-0007`，两者均回滚到服务
savepoint，不产生部分配置或残留预留记录。

无字段变化时返回 `changed=false`，版本保持不变。成功变化只递增一次版本；同键同参
重放返回首个领域结果，但使用新的请求 ID。

## 权限与审计

DocType、服务和工作区只授予：

- `System Manager`
- `HRP System Manager`

两个角色均可读、创建和写入 Single DocType，但不能删除。角色校验发生在配置读取和
幂等记录创建之前。

审计只记录事件名、配置版本、变化字段名、变化字段数量以及默认项是否已配置。
它不记录法人、医院、说明或幂等键原文。幂等响应由 Site 密钥加密，记录中只保存请求
摘要。

## 安装、迁移与回滚

`after_install` 与 `after_migrate` 调用幂等的 `ensure_system_settings()`：

1. 恢复三个不可弱化的固定策略；
2. 初始化配置版本和集成超时；
3. 清理缺少默认法人的无效医院默认值；
4. 第二次运行不再写入或递增版本。

升级前应按平台发布流程备份 Site 数据库。回滚代码前必须确认旧版本能够识别当前字段；
本任务只增加 `tabSingles` 字段，不创建业务表。若需要回退，可先导出当前显式配置，
回滚应用提交并运行 `bench --site <site> migrate`。不要删除 `tabSingles` 中的历史字段，
它们不会被旧版本读取。

## 验证

```bash
python -m unittest tests.test_system_settings -v
python scripts/change_manager.py validate
python scripts/change_manager.py check \
  --base-ref origin/main \
  --task COD-017 \
  --correlation-id COD-017-local
python scripts/quality.py
```

锁定 Bench 集成测试还必须覆盖真实 Single DocType 元数据、迁移幂等、角色先行、公司
引用、版本冲突、无操作、同键重放/冲突、固定策略拒绝、审计脱敏以及 GET/POST HTTP
契约。
