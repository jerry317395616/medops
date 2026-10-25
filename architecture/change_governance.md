# ADR 与变更治理

## 权威边界

工程决策必须在实现和数据库迁移之前可评审，因此 Git 是 ADR 与代码变更记录的
唯一权威。Frappe Site 不保存可编辑副本，也不提供批准或变更状态的 HTTP 写
接口：

- `medops/config/change_governance.json`：状态机、风险等级、受保护路径；
- `architecture/adr`：严格 front matter 和固定章节的可读 ADR；
- `changes/COD-XXX.json`：任务、风险、影响模块、迁移、回滚、权限、安全和测试；
- `backlog/backlog.csv`：任务状态与依赖；
- Git 分支、Pull Request、CI 和提交历史：身份、评审和不可变审计证据。

`medops.services.change_governance` 只向 `System Manager` 或
`HRP System Manager` 返回脱敏摘要。API 不返回绝对路径、ADR 正文、决策人、
Git 命令输出或凭据。

## ADR 生命周期

ADR 文件名为 `ADR-NNNN-kebab-case.md`，front matter 字段必须与
`architecture/ADR_TEMPLATE.md` 完全一致。允许的状态转换为：

```text
Proposed -> Accepted
Proposed -> Rejected
Accepted -> Superseded
```

`Rejected` 和 `Superseded` 是终态。Accepted ADR 的标题、日期、决策人、关联
任务、正文和历史关系不可修改；取代决策时只允许把原 ADR 改为 `Superseded`，
设置 `superseded_by`，并由新 ADR 的 `supersedes` 反向引用。ADR 不允许删除、
改名或复用编号。

新 ADR 至少包含两个真实备选方案，并完整说明背景、驱动因素、决策、后果、
验证、退出条件、迁移、回滚、安全和合规。受保护路径或破坏性变更只能引用
直接关联当前 COD 的 Accepted ADR。

## 变更记录

每个状态为 `Done` 的 backlog 任务必须有且仅有一个
`changes/COD-XXX.json`。记录使用严格 schema，未知字段和缺失字段都会失败。
必须声明：

- 风险等级与变更类型；
- 受影响的 36 个已登记模块；
- 是否为破坏性变更及关联 ADR；
- 可覆盖本次 Git 差异的仓库相对路径；
- 权限、迁移、回滚和安全影响；
- 可执行测试计划、责任团队和日期。

一个 PR 只允许修改一个 COD 变更记录，并必须同时修改对应任务文档和 backlog
状态。所有新增、修改和删除路径都要被该记录覆盖。路径规则只允许仓库相对
glob，不接受绝对路径、反斜杠或 `..`。

## 风险门禁

风险从低到高为 `low`、`medium`、`high`、`critical`。机器策略根据真实差异
提升最低风险，记录可以高报但不能低报：

| 类别 | 最低风险 | Accepted ADR |
| --- | --- | --- |
| 单应用边界、模块注册、锁定上游 | critical | 必需 |
| 分支保护和运行时权限 | critical | 必需 |
| ADR、变更策略、CI 与评审门禁 | high | 必需 |
| DocType、Patch、Fixture、设计目录 | high | 破坏性时必需 |
| Service 与 API | medium | 非破坏性时可选 |

任何 `breaking_change=true` 的记录都必须引用 Accepted ADR。风险规则匹配多个
类别时采用最高等级。

## 本地流程

从第一个未完成任务创建短期分支，并先建立变更记录：

```bash
python scripts/change_manager.py validate
python scripts/change_manager.py plan --base-ref origin/main --task COD-008
python scripts/change_manager.py check \
  --base-ref origin/main \
  --task COD-008 \
  --correlation-id COD-008-local
python scripts/quality.py
```

`validate` 检查完整仓库；`plan` 只读展示当前差异；`check` 执行相同确定性评估并
在 Git 目录写入权限为 `0600` 的脱敏 JSONL 审计。重复计划的治理摘要和变更
摘要必须一致。审计只记录关联 ID、任务 ID、成功/失败类型和 SHA-256，不记录
文件正文、命令输出、环境变量或用户名。

## CI 与合并

Pull Request 使用 base commit，`main` 推送使用事件 before commit。Quality
作业先验证完整仓库，再对真实 Git 差异运行 `change_manager.py check`。手工
触发且没有可用 base commit 时只执行完整仓库验证。CI 不读取生产数据库、不
连接 Press，并保持 `contents: read`。

PR 必须包含 COD 编号、ADR/变更记录、测试证据、迁移影响和回滚方法。只有
Quality、Security、Integration、Required 均成功后才能合并。

## 数据迁移与回滚

治理源文件随 App 版本发布，但不创建 DocType、Fixture、Patch 或业务记录。
回滚代码不会删除 Git、PR 或 CI 历史。若某项已实施变更需要业务回滚，将变更
记录标记为 `Rolled Back`，保留原记录并通过新的 COD 执行回滚；禁止改写历史
提交或删除 Accepted ADR。
