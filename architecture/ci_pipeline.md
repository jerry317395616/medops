# CI 流水线

## 目标与边界

COD-005 在 GitHub Actions 中建立 `medops` 的合并门禁。流水线只读取代码并
创建一次性测试环境，不连接 Press、生产数据库、对象存储或外部业务系统，也
不使用仓库 Secret。它不会修改 Frappe、ERPNext 或 Frappe HR 源码。

## 触发与并发

`.github/workflows/ci.yml` 在以下事件运行：

- Pull Request；
- 合并到 `main` 后；
- 人工 `workflow_dispatch`。

同一 PR 或分支的新运行会取消旧运行，所有作业都有明确超时。工作流权限仅为
`contents: read`，禁止 `pull_request_target`，防止不受信任的 PR 取得高权限
令牌。

## 作业

### Quality

`Quality` 使用 Python 3.14 和 Node.js 24：

1. 从精确版本声明安装 Python 开发依赖，并以 `npm ci` 恢复 Node 锁文件；
2. 运行 `scripts/quality.py` 的仓库契约、包校验、编译、Ruff、Bandit、
   仓库测试、校验和、Pyright、ESLint 和 Prettier；
3. 下载锁定的官方 k6 `2.1.0` Linux 发布包，校验 SHA-256 并对性能脚本执行
   `k6 inspect`，不连接站点、不发送压测请求；

### Security

`Security` 在 `Quality` 成功后运行，并与 `Integration` 并行。它检出完整 Git 历史，
安装固定 Bandit、pip-audit 和 npm，下载固定 Gitleaks、Grype、CycloneDX CLI，
并在 SHA-256 校验成功后执行：

1. npm CycloneDX 依赖图和 pip-audit Python 依赖解析；
2. 组合 `medops` 与三个锁定上游，生成并验证 CycloneDX 1.7；
3. Bandit、Gitleaks、pip-audit、npm audit、Grype 和许可证门禁；
4. 上传 SBOM、脱敏摘要和 SHA-256 清单，保留 30 天。

原始发现不上传，作业不读取仓库 Secret，也不连接 Frappe Site 或生产系统。详细
契约见 `architecture/software_supply_chain.md`。

### Integration

`Integration` 只在 `Quality` 成功后运行。它使用 MariaDB 11.8 服务容器和
本机临时 Redis，通过 `scripts/ci_integration.sh`：

1. 从 `resolved_versions.lock.json` 获取并检出 Frappe、ERPNext、Frappe HR
   的不可变提交；
2. 创建新的 Bench 与 `test_site`，安装 ERPNext、Frappe HR 和唯一自研 App
   `medops`；
3. 再次验证上游锁、执行幂等迁移和全部 `medops` Frappe 集成测试；
4. 验证新站点没有 Error Log，四个 App 工作树均保持洁净。

临时站点使用固定的非生产测试密码，运行结束后随 GitHub Runner 销毁。失败时
只保留 Bench 日志 7 天；日志不得写入凭据或业务敏感数据。

### Required

`Required` 使用 `always()` 汇总前三个作业。只有 `Quality`、`Security` 和
`Integration` 都为 `success` 才返回成功，因此前置作业失败、跳过或取消都不能
绕过门禁。
GitHub API 中的稳定检查上下文名为 `Required`，页面显示为
`CI / Required`；它是 `main` 分支唯一要求的 CI 状态检查。

## 供应链约束

第三方 Action 必须固定到完整 40 位提交 SHA，禁止 `@main`、`@v6` 等浮动
引用。Python 开发依赖使用精确版本，Node 依赖由 `package-lock.json` 完整
锁定；Frappe 生态依赖仍只接受 `resolved_versions.lock.json` 的三个提交。
仓库契约对触发器、权限、作业名、超时、依赖关系、命令、k6 版本/发布包摘要、Action 引用和分支
保护上下文执行失败关闭校验。

## 本地与隔离验证

本地执行：

```bash
python -m pip install -e ".[dev]"
npm ci
python scripts/quality.py
python scripts/security_supply_chain.py plan
```

具备 MariaDB、Redis、uv、Python 3.14 和 Node.js 24 的一次性 Linux 环境可执行：

```bash
BENCH_DIR=/tmp/medops-ci \
SITE_NAME=test_site \
DB_ROOT_PASSWORD=ci_root_password \
ADMIN_PASSWORD=ci_admin_password \
PYTHON_BIN=python3.14 \
bash scripts/ci_integration.sh
```

## 回滚

回滚工作流、`scripts/ci_integration.sh`、分支保护策略和对应契约测试即可恢复
COD-004 状态。该任务没有 DocType、Patch、Fixture 或生产数据迁移，禁止以
关闭必需检查的方式临时绕过失败；应修复失败原因后重新运行。

## 完整集成的执行预算

按 ADR-0038，普通和 IONE Core 集成作业各使用120分钟时限，仓库契约拒绝低于120的配置。Quality、Security、Required及全部原生、并发和Error Log检查继续必跑；超时或缺证仍是失败。该预算只约束CI，不改变确认凭证窗口、生产请求或数据库锁超时。原生测试分组须另行证明发现范围、状态隔离和失败传播等价。

## Core 完整资源构建与源码证明

按 ADR-0042，新 Bench 继续构建全部应用；Core 的 Vite 输出不再被当作源码修改，也不能整体豁免。`scripts/core_build_integrity.py` 在构建前检查固定提交和洁净工作树/索引；构建后只准固定配置对应的有限输出路径变化，并验证入口副本与两个公共文件。保存源码及全部生成文件的内容摘要/执行位清单；CI 父进程在测试前保留证明摘要，最后核对原摘要及实际文件清单。测试后不重置文件。其他四个应用保持原有 Git 洁净要求。完整40719053510曾因这些生成输出在最后门禁失败，虽然 Core 业务测试已通过；该失败保留，新候选须完整重验。
