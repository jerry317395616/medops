# 开发、测试与演示环境

## 目标

COD-006 为 `medops` 建立可重复的非生产环境。默认拓扑是一种环境一个
Bench、一个 Site 和一个数据库用户；三个 Bench 使用相同的
`resolved_versions.lock.json`，但目录和全部服务端口互不复用。任何配置档
都不得指向 `manager.myyr.top`、生产目录或生产数据。

机器权威为 `medops/config/environment_profiles.json`。环境管理器和运行时
状态服务共同读取该文件，不维护第二套配置。

## 环境矩阵

| 配置档 | 默认 Site | Web 端口 | 开发模式 | 测试接口 | 调度器 | 数据 |
| --- | --- | ---: | --- | --- | --- | --- |
| `development` | `hrp-dev.localhost` | 8100 | 开启 | 开启 | 开启 | 仅合成 |
| `test` | `hrp-test.localhost` | 8200 | 关闭 | 开启 | 关闭 | 仅合成 |
| `demo` | `hrp-demo.localhost` | 8300 | 关闭 | 关闭 | 关闭 | 仅合成 |

三个配置档都关闭邮件队列、外部集成和公开访问。Redis cache、Redis queue、
Socket.IO 和文件监听端口也分别唯一。演示站点由
`medops.setup.demo.setup_synthetic_demo` 调用 ERPNext 标准 Setup Wizard
控制器创建 `MedOps Synthetic Demonstration Hospital`；重复执行返回
`changed=false`，存在其他公司时拒绝运行。

## 准备

需要 Linux、MariaDB、Redis、Node.js 24、Python 3.14、`uv` 和 Git。秘密只从
进程环境读取：

```bash
cp .env.example .env
# 修改两个密码，禁止使用示例值
set -a && source .env && set +a
```

先验证配置和只读计划：

```bash
python scripts/environment_manager.py validate
python scripts/environment_manager.py plan development
python scripts/environment_manager.py plan test
python scripts/environment_manager.py plan demo
```

计划输出不含密码、令牌或服务器私有配置。默认 Bench 位于
`~/.local/share/medops/environments/<profile>/frappe-bench`。

## 创建和运行

```bash
python scripts/environment_manager.py provision development
python scripts/environment_manager.py provision test
python scripts/environment_manager.py provision demo
```

首次运行创建锁定 Bench、安装 ERPNext、Frappe HR 和唯一自研 App
`medops`，随后写入站点级安全配置、设置调度器并迁移。目标已存在时不会
重建或清空数据库，只校验锁定提交和已安装 App，再幂等应用同一配置。

演示站点完成 ERPNext 合成机构初始化：

```bash
cd ~/.local/share/medops/environments/demo/frappe-bench
bench --site hrp-demo.localhost execute medops.setup.demo.setup_synthetic_demo
```

启动某个 Bench：

```bash
cd ~/.local/share/medops/environments/development/frappe-bench
bench start
```

浏览器地址分别为 `http://hrp-dev.localhost:8100`、
`http://hrp-test.localhost:8200` 和 `http://hrp-demo.localhost:8300`。

## 校验与运行时可见性

```bash
python scripts/environment_manager.py verify development
python scripts/environment_manager.py verify test
python scripts/environment_manager.py verify demo
```

`verify` 同时检查上游锁定提交、四个已安装 App、站点配置漂移和调度器状态。
已认证用户可只读调用：

```text
GET /api/method/medops.api.v1.environment.get_environment_status
```

响应不包含 Bench 路径、端口、数据库名或秘密；未受管理的生产站点只报告
`managed=false`，不会猜测生产策略。

## 审计、隔离和失败处理

- `provision`、`configure`、`verify` 使用格式受限的 correlation ID。
- 成功和失败写入 Bench `logs/environment-audit.jsonl`；文件权限为 `0600`，
  不记录密码、令牌、命令参数或业务数据。
- 覆盖默认目标必须显式添加 `--allow-target-override`；生产式域名、目录和非
  `.localhost` Site 在任何情况下都被拒绝。
- 已绑定其他配置档的 Site 不允许重新归类。
- 新建失败不会切换任何生产站点；已有 Bench 但缺少预期 Site 时拒绝修复，
  避免把部分环境误当成干净环境。
- 外发邮件和外部系统调用默认关闭。业务集成接入时必须调用
  `assert_external_integrations_allowed()`，不能仅依赖界面隐藏。

## CI 证据

`scripts/ci_integration.sh` 在一次性 MariaDB 11.8 Runner 中构建锁定 Bench，
分别创建开发、测试、演示三个数据库 Site。测试配置连续应用两次并验证第二次
`changed=false`；演示基线也连续执行两次。随后检查三个配置档、应用测试、
Error Log、脱敏审计和四个 App 工作树。CI 为节省构建时间共享临时代码 Bench，
正式配置仍是一环境一 Bench。

验收证据为 GitHub Actions
[`30436254750`](https://github.com/jerry317395616/medops/actions/runs/30436254750)：
56 项仓库契约与单元测试、18 项 Frappe 集成测试通过；三个 Site 均无配置漂移
且 `Error Log` 为 0；测试配置和演示数据重复执行均保持幂等。该运行使用提交
`21fcaa3fc0c389445964a194e15103ec66fbeb5e`，三个锁定上游工作树均为干净状态。

## 升级、重置与回滚

环境代码升级必须走 PR、Required CI 和锁定版本校验。非生产环境可按
`reset_policy=replaceable` 删除并重建，但删除前必须确认目标来自配置档并保留
需要的合成测试快照；环境管理器故意不提供自动删除命令。

COD-006 没有 DocType、Patch 或生产数据库迁移。回滚代码时撤销配置档、管理器、
状态 API 和 CI 扩展；环境 Site 需由运维按完整路径人工停机后删除。不得把
非生产数据库恢复到生产，也不得从生产备份填充演示环境。
