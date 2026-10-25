# medops 仓库与分支治理

## 范围

本仓库是医院 HRP 的唯一 mono-repo，也是可被 Press 直接克隆的标准 Frappe
App 仓库。自研代码只能位于根目录的 `medops` Python 包，业务边界使用
该 App 内部的 Frappe Module 和
Python 包表达。`frappe`、`erpnext`、`hrms` 是锁定提交的上游依赖，不纳入
本仓库，也不得修改其核心源码。

## 分支模型

- `main` 是唯一长期分支和可发布基线。
- 所有变更从短期任务分支发起，格式为 `cod/COD-XXX-short-name`。
- `main` 禁止直接推送、强制推送和删除。
- 合并必须使用 Pull Request，保持线性历史并解决全部评审讨论。
- 当前仓库只有一名维护者，采用“强制 PR、暂不强制独立批准”的可执行基线。
- `CODEOWNERS` 继续声明代码所有权；添加第二名维护者后，把必需批准数提升为 1。

`.github/branch-protection.json` 是机器可读的保护策略；GitHub 中的实际规则
必须与该文件保持一致。仓库管理员也受同一规则约束。

GitHub 仓库已由所有者明确批准并设置为公开仓库。GitHub Free 的服务端分支
保护已经生效，实际规则必须持续通过脚本与策略文件比对。

执行以下命令应用并核验远端策略：

```bash
python scripts/apply_branch_protection.py --apply
```

开发机执行 `git config core.hooksPath .githooks` 启用版本化的
`pre-push` 防护。该本地钩子只用于降低误推风险，不能替代 GitHub 服务端
保护，也不能作为 COD-001 完成证据。

COD-005 已建立 GitHub Actions 流水线；`main` 分支保护必须要求 API 上下文
`Required`（GitHub 页面显示为 `CI / Required`）。该检查只有仓库质量、前端
检查、秘密与依赖扫描、锁定 Bench 迁移和 `medops` 集成测试全部成功时才通过。

## 单应用边界

1. 只允许仓库根目录的 `medops` 一个自研 App。
2. 新业务域登记到 `architecture/module_registry.yaml` 和
   `medops/modules.txt`，并创建对应模块包。
3. 禁止创建 `medops_budget`、`medops_supply` 等独立 App。
4. 跨模块调用通过公开 service facade 或领域事件。
5. 禁止直接写 `GL Entry`、`Stock Ledger Entry`、`Bin`。
6. 标准业务过账必须调用 ERPNext/Frappe HR 标准控制器。

## 变更流程

1. 从 backlog 选择一个未完成的 `COD-XXX`，一次只处理一个任务。
2. 创建或更新 `changes/COD-XXX.json`；受保护路径和破坏性变更同时创建或引用
   Accepted ADR。
3. 在任务分支完成模型、服务、权限、API、测试、迁移和文档中适用的部分。
4. 运行：

   ```bash
   python scripts/change_manager.py validate
   python scripts/change_manager.py check --base-ref origin/main --task COD-XXX
   python scripts/repository_contract.py
   python scripts/validate_package.py
   python scripts/checksums.py
   python -m unittest discover -s tests -p "test_*.py"
   ```

5. 使用 PR 模板记录 ADR、风险、设计依据、测试证据、权限/安全、迁移影响和
   回滚方法。
6. 合并后由锁定提交构建 Press Bench；生产站点只执行经过备份验证的迁移。

ADR、变更记录、状态机、风险升级和差异覆盖的完整契约见
`architecture/change_governance.md`。Git/PR/CI 是唯一写入和审计通道；
Frappe 只提供管理员只读状态，不允许从生产 Site 改写架构决策。

Press 不支持从 Git 仓库子目录加载 App，因此 `pyproject.toml` 必须位于仓库
根目录，且 `medops/hooks.py` 必须可被 Press 的 App Source 直接发现。

## COD-001 适用性说明

COD-001 是仓库治理任务，不新增业务 DocType、写 API、Workspace、Fixture 或
数据库迁移，因此模型、服务、权限和业务幂等不适用。异常路径由仓库契约测试
覆盖，包括多 App、旧前缀、弱化分支保护和受保护台账写入模式。
