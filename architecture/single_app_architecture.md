# 单应用多模块架构

## 决策

HRP 自研能力统一放入 `medops` 一个 Frappe App。Frappe、ERPNext 和 Frappe HR 仍是独立上游依赖。业务边界使用 Frappe Module、Python 包、领域服务、权限和事件隔离，而不是拆成九个自研 App。

该决策由
`architecture/adr/ADR-0001-single-application-architecture.md` 正式记录。
改变 App 边界必须创建新的 Accepted ADR，不能只修改模块注册表或
`pyproject.toml`。

## 为什么采用一个 App

- 一次安装、一次迁移、一次版本发布，降低医院项目交付复杂度。
- DocType Link、Workflow、Workspace、报表和权限可在同一 App 内原子升级。
- Codex 可以按模块工作，同时共享统一基础服务。
- 避免九个自研 App 的依赖环、安装顺序、版本矩阵和跨 App Patch 管理。

## 边界约束

一个 App 不等于无边界。必须遵守：

1. 模块 DocType 位于对应模块包。
2. 跨模块调用通过公开 service facade 或领域事件。
3. 禁止模块直接导入另一模块的私有 `doctype` 控制器。
4. 基础能力向业务模块单向依赖；业务模块不得反向污染基础层。
5. 财务和库存过账仍委托 ERPNext 标准单据控制器。
6. 人事、薪资和考勤标准能力优先复用 Frappe HR。

## 目录模式

```text
pyproject.toml
architecture/
└── module_registry.yaml
medops/
├── hooks.py
├── modules.txt
├── common/
├── services/
├── integrations/
├── setup/
├── api/v1/
├── hrp_foundation/
├── hrp_budget/
├── hrp_procurement/
└── ...
```

仓库根目录即 Frappe App 根目录，使 Press 可以直接克隆、审核、构建和更新。

## 模块新增流程

1. 在 Bench 中运行 `bench medops-create-module ... --yes`，或在开发包根目录运行 `scripts/create_module.py`。
2. 审查 `modules.txt`、模块包和 `module_registry.yaml`。
3. 在模块目录中创建标准 DocType/Report/Page/Workspace。
4. 运行 `bench --site <site> migrate`；`after_migrate` 会补齐 Module Def 与 HRP Module Setting。
5. 提交代码、迁移、权限、测试和文档。

生产界面只允许启停已发布模块，不允许生成源代码模块。

## 模块注册表契约

`architecture/module_registry.yaml` 是模块显示名、Python 包名、领域组、中文
名称、默认启用状态和顺序的唯一机器权威。`medops.services.module_registry`
负责解析和严格校验，以下入口不得自行实现另一套解析规则：

- 安装和迁移阶段的 `Module Def`、`HRP Module Setting` 同步；
- 模块查询与启停 API；
- `bench medops-create-module` 和 `scripts/create_module.py`；
- 仓库契约、打包校验与自动化测试。

当前基线必须恰好登记 36 个模块。注册表、`modules.txt`、模块目录、
`__init__.py`、`README.md` 和七个标准子包必须逐项一致，存在重复名称、重复
序号、未登记目录、缺失目录或包名不匹配时，构建立即失败。
仓库契约还会解析 Python 导入；跨模块调用只能导入目标模块的 `services`
公共门面，禁止直接依赖另一模块的 DocType 控制器或其他内部包。

模块生成是开发期源码操作。生成器在写入前验证现有源码树，使用临时目录和
原子文件替换；任何后置校验失败都会恢复 `modules.txt`、注册表并删除新目录。
生产站点只同步已发布模块元数据，保留各站点自己的启停选择。
