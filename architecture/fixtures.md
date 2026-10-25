# Fixtures 导出与同步规范

## 适用范围

Fixtures 是随 App 安装和迁移同步的数据库配置记录，不是主数据、交易数据、
环境配置或演示数据。Frappe 的 `bench --site <site> export-fixtures --app medops`
会按 `hooks.py` 导出记录，安装及迁移时自动从 `medops/fixtures` 导入。

以下内容不得改用 Fixtures：

- `medops` 自有 DocType、Workspace、Report、Page 等标准对象，应保存在所属
  Module 的标准 JSON/Python/JavaScript 文件中。
- 组织、用户、患者、供应商、物资、会计、库存、单据和日志属于业务或运行数据。
- Module Def 和四个核心 Role 由安装服务幂等创建。
- 环境 Site 配置、密码、令牌、邮件账号和第三方连接不得进入 App 源码。
- 一次性数据修复或结构迁移必须使用可审计 Patch，而不是 Fixture。

Frappe 官方 Fixtures 机制见
[Hooks / Fixtures](https://docs.frappe.io/framework/user/en/python-api/hooks#fixtures)。
本仓库在标准导出之上增加归属、确定性和敏感数据门禁。

## 唯一权威与白名单

机器权威为 `medops/config/fixture_policy.json`。`hooks.py` 通过
`get_frappe_fixture_hooks()` 从同一策略生成配置，禁止复制维护第二套过滤器。

| 顺序 | DocType | 归属过滤 | 用途 |
| ---: | --- | --- | --- |
| 1 | Custom Field | `module` 属于 `medops/modules.txt` | 扩展标准 DocType 字段 |
| 2 | Property Setter | `module` 属于 `medops/modules.txt` | 最少量标准元数据覆盖 |
| 3 | Custom DocPerm | `role` 属于四个 HRP 核心角色 | 标准 DocType 的附加权限 |

`fixture_auto_order = True` 固定同步文件名和依赖顺序。新增 Fixture 类型必须同时：

1. 证明不能用标准 Module 文件、安装服务或 Patch。
2. 提供不可伪造的归属字段及有限值来源。
3. 定义依赖顺序、权限、幂等、异常和全新 Site 同步测试。
4. 更新策略、hooks、文档、API 契约、仓库契约和审计测试。
5. 经单独 PR 审查；不得临时放宽为“导出全部记录”。

## 确定性与敏感数据

受控导出会连续执行两次 Frappe 标准 `export-fixtures`。每次导出后：

- 删除 `creation`、`modified`、`modified_by`、`owner`、`idx`、`lft`、`rgt`
  等易变字段。
- 按稳定 `name` 排序，并以 UTF-8、排序键和统一换行写入。
- 验证每条记录的 DocType、唯一名称和模块/角色归属。
- 拒绝策略外文件、目录、DocType、归属值、敏感字段和密钥特征。
- 比较两次 SHA-256；结果不同则失败，不允许提交。

显式禁止用户、角色分配、用户权限、文件、邮件、通信、访问/错误/任务日志、
OAuth、集成请求、系统设置和认证表。扫描会拒绝密码、API 密钥、令牌、私钥、
授权码、数据库/SMTP 密码等字段或值。业务数据即使未命中扫描也不得导出；
白名单归属过滤是第一道门禁。

## 操作流程

只读检查可在任意源码工作树运行：

```bash
python scripts/fixture_manager.py plan
python scripts/fixture_manager.py validate
```

导出必须从已安装此源码的受管理 development Bench 执行：

```bash
cd /path/to/frappe-bench/apps/medops
python scripts/fixture_manager.py export \
  --bench-dir /path/to/frappe-bench \
  --site hrp-dev.localhost \
  --correlation-id COD-007-change-001 \
  --yes
```

命令要求：

- Site 必须以 `.localhost` 结尾，并声明
  `medops_environment=development`、`developer_mode=1`、`allow_tests=1`。
- Bench 中的 `apps/medops` 必须就是当前源码工作树。
- 当前 Fixture 文件必须已提交且无未提交改动。
- 运行时服务再次确认 development 配置和外部集成关闭。
- `--yes` 仅表示已审查本次源码写入，不绕过任何安全门禁。

导出后审查 `git diff -- medops/fixtures`，运行完整质量门禁，再通过 PR 合并。
禁止在生产、测试或演示 Site 导出，禁止从生产复制记录后再“清理”提交。

## 权限、API 与审计

只读治理状态：

```text
GET /api/method/medops.api.v1.fixtures.get_fixture_governance_status
```

仅 `System Manager` 可调用；Guest 和普通 HRP 用户拒绝。响应只返回策略版本、
规则、文件/记录数和摘要，不返回源码路径、具体归属值或记录内容。HTTP 不提供
导出或文件写接口。

成功或失败的实际导出写入 Bench
`logs/fixture-export-audit.jsonl`，权限为 `0600`。事件只包含时间、Site、
correlation ID、状态、变更标记、文件/记录数和摘要，不包含路径、命令输出、
Fixture 内容、密码或令牌。

## CI 证据

GitHub Actions
[`30443592308`](https://github.com/jerry317395616/medops/actions/runs/30443592308)
在锁定 Bench 中安装三个 Site，并运行 22 项 Frappe 集成测试。development
Site 的连续两次标准导出均规范化为 3 个文件、0 条记录，返回
`changed=false`、`idempotent=true`，摘要为
`32c7daeffbcbac3dd3a19888a82c08d0e0c14b8066ce2089c054dcfc66034978`。
审计敏感标记、三个 Site Error Log 和四个 App 工作树检查均通过。

## 安装、升级与回滚

Fixtures 由 Frappe 标准安装/迁移控制器同步。Custom Field、Property Setter 和
Custom DocPerm 都是配置覆盖；不直接写 GL Entry、Stock Ledger Entry 或 Bin，
也不修改已提交业务单据。

回滚时撤销对应 Fixture JSON 和策略/hooks 变更，再在非生产 Site 迁移验证。
仅从策略删除记录不会自动删除目标 Site 已存在配置；需要删除时必须提交显式、
幂等、可回滚 Patch，并先评估字段数据和权限影响。生产推广、删除 Patch 和业务
数据迁移不属于 COD-007。
