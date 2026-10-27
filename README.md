# 医衡 MedOps 医院运营与资源管理

MedOps 是原 `ione_hrp` HRP项目的新名称，继续提供医院运营与资源管理能力，
作为美妍伊人医疗生态的第一个自研项目。整体医疗开发站点与此应用分别管理。
正式依赖优先复用Frappe生态标准应用；所有应用Git来源必须位于
`https://github.com/jerry317395616/`，不从官方仓库直接克隆或拉取。

本次更名只在全新空站安装，不是既有ione_hrp站点的原地迁移程序。原有HRP
DocType和36个模块保持业务边界；详见 `architecture/medops_identity.md`。

本包把原先的九个自研 Frappe App 合并为一个应用：`medops`。所有医院 HRP 业务能力通过 **36 个标准 Frappe Module** 组织，并共享同一套安装、版本、权限、事件、接口、测试和发布生命周期。仓库的默认分支为 `main`，治理规则见 `architecture/repository_governance.md`。

## 源码基线

- Frappe：`jerry317395616/frappe@4880ad14d2b067e8db1a8356ef2d325713cc3cf4`
- ERPNext：`jerry317395616/erpnext@372dff2ffa232f54595f48c2639d828c4a64ddde`
- Frappe HR：`jerry317395616/hrms@370b052071a63d89640c4a706faf61b40561e748`
- 三项依赖保持原 v17 功能基线；Frappe和HRMS仅替换依赖来源地址，版本标识均为 `17.0.0-dev`
- Python：`>=3.10,<3.15`（兼容当前 Press v17 运行时）
- 默认数据库：MariaDB

`develop` 是滚动/nightly 源码，不能直接作为可重复部署基线。
`resolved_versions.lock.json` 是唯一机器权威；初始化脚本即使从 `develop`
克隆，也会在创建 Site 前切换到锁定 SHA 并验证远程地址、版本、工作树和
提交。更新基线必须单独提交锁文件、完成隔离 Bench 迁移测试，再经 PR 推广，
禁止部署时自动改写锁文件。

## 核心目录

```text
pyproject.toml                  Press/Frappe 应用元数据
medops/                       单一 Frappe 应用源码
architecture/                 单应用架构、模块注册表和版本策略
design/                       401 个 DocType 与字段等机器可读设计
doctype_blueprints/           按模块重组的 401 份 DocType 蓝图
api/ workflows/ backlog/      API、工作流和 Codex 任务
scripts/                      初始化、安装、加模块、版本锁定和校验脚本
AGENTS.md                      Codex 仓库级强制规则
```

## HRP 系统设置

`HRP System Settings` 是 `HRP Foundation` 拥有的全站 Single DocType。它以显式
字段管理启用状态、默认法人/医院、集成超时和说明；发布通道、严格数据域及 AI
人工确认固定为不可弱化的代码策略。系统管理角色可通过 `PLT-020` 读取，通过要求
`Idempotency-Key` 与 `expected_version` 的 `PLT-021` 更新。

写入使用 `tabSingles` 行锁、单调配置版本、加密幂等结果和仅字段名/版本的脱敏审计，
没有任意 JSON 或动态代码入口。完整模型、权限、接口、迁移和回滚规则见
`architecture/system_settings.md`。

## 组织层级与版本

`HRP Organization` 以 `HRP Hospital`、`HRP Organization Version` 和
`HRP Organization Unit` 保存医院组织。每个版本包含一棵完整 Nested Set 树，通过
`CORE-009` 至 `CORE-012` 的幂等领域服务创建、整体替换和发布；发布后版本与节点不可
修改或取消。`CORE-013` 可按显式版本或医院与业务日期查询确定快照。

COD-017 的默认医院已升级为正式 Link；旧文本在迁移时确定性转换为医院记录并保留原名称。
COD-019 通过只读 `HRP Organization Mapping` 把发布版本内的组织单元映射到同一法人的
ERPNext 标准 `Department`/`Cost Center`。`CORE-014` 负责幂等修订并校验启停、唯一性和
双树关系，`CORE-015` 按组织单元或业务日期确定性解析；标准主数据仍完全由 ERPNext
控制器维护。完整模型、权限、接口、迁移和回滚规则见
`architecture/organization_hierarchy.md`、`architecture/organization_mapping.md`、
`architecture/adr/ADR-0012-versioned-hospital-organization-hierarchy.md`、
`architecture/adr/ADR-0013-version-scoped-standard-organization-mapping.md` 与
`architecture/adr/ADR-0014-governed-master-data-change-proposals.md`。

## 主数据治理

`HRP Master Data Domain` 为 `Department`、`Cost Center`、`Item`、`Supplier` 和
`Warehouse` 定义静态治理边界；`HRP Master Data Request` 及其字段提案子表只保存
结构化变更建议、目标基线和复核结果。`CORE-016` 至 `CORE-020` 提供领域维护、草稿、
提交、批准、拒绝和查询能力，并执行幂等、行锁、乐观修订、目标漂移、组织范围和申请人
与审核人职责分离校验。批准只形成治理结论，不直接修改 ERPNext 标准主数据；后续执行
必须继续调用对应标准控制器。完整规则见 `architecture/master_data_change_requests.md` 与
`architecture/adr/ADR-0014-governed-master-data-change-proposals.md`。

主数据访问同时检查业务角色、当前法人/医院/科室范围、真实 ERPNext 目标读取及字段权限。
全局主数据域只允许系统管理角色维护；普通账号须显式配置范围与必要的标准目标读取或共享。
“主数据中心”通过“主数据受控查询”进入申请、映射、质量规则与问题，支持过滤和可见记录分页；
四种模型的通用 SQL 列表仅 Administrator 可用。历史质量结论保留不可缩减的字段读取约束，
旧幂等键只允许原主体在当前仍获授权时重试。迁移、授权配置和回退见
[核心主数据访问说明](architecture/core_data_access.md) 与 ADR-0026。

## 外部编码映射

`HRP External Code Mapping` 在外部系统编码与受控 ERPNext 标准主数据之间提供一对一双向解析。
外部编码保留前导零和大小写，映射按法人、医院、可选组织单元、主数据域、外部系统和显式业务日期
确定。源身份和目标身份分别生成版本化 SHA-256 键并由数据库唯一约束保护，避免 MariaDB 长复合
索引限制和并发重复。

`CORE-021` 是唯一写入口，要求治理角色、`Idempotency-Key`、行锁和乐观修订；`CORE-004` 与
`CORE-022` 分别负责入站和出站只读解析。映射 DocType 在 Desk 中只读，标准 `Department`、
`Cost Center`、`Item`、`Supplier`、`Warehouse` 仍完全由 ERPNext 控制器维护。完整模型、权限、
审计、迁移和回滚规则见 `architecture/external_code_mapping.md` 与
`architecture/adr/ADR-0015-service-owned-bidirectional-external-code-mapping.md`。

## 主数据质量

`HRP Data Quality Rule` 使用五类声明式白名单规则检查受控 ERPNext 标准主数据，既不执行用户
SQL、Python 或任意正则，也不修改标准记录。`HRP Data Quality Issue` 按“规则 + 目标记录”维护
唯一问题：失败时创建或重开，通过时自动解决；观察值只保存摘要且不进入公开响应。

`CORE-023` 负责幂等创建和修订规则，`CORE-024` 评估单条记录并维护问题，`CORE-025` 查询脱敏
问题。每日调度按规则排队，目标记录每批最多 200 条。两个 DocType 在 Desk 中只读，完整模型、
安全边界、迁移和回滚规则见 `architecture/data_quality_rules.md` 与
`architecture/adr/ADR-0016-declarative-master-data-quality-rules.md`。

## 统一编号

`HRP Numbering Scheme` 以声明式白名单模板定义日期、受控维度、流水号位数和重置周期；
`HRP Number Reservation` 永久保存已发编号、预留令牌、方案修订和审计上下文。模板不允许
SQL、Python 或任意表达式，首个预留产生后不能改变方案格式。

`CORE-006` 在单一事务中按方案、日期分桶和规范维度原子递增 Frappe 序列，并写入全局唯一、
不可修改和不可删除的预留记录。同一 `Idempotency-Key` 重放返回原编号而不再次递增；
`CORE-026` 维护方案，`CORE-027` 按所有者或审计权限查询预留。完整并发、权限、审计、迁移和
回滚规则见 `architecture/unified_numbering.md` 与
`architecture/adr/ADR-0017-atomic-unified-number-reservations.md`。

## 访问范围与数据权限

`HRP Access Scope` 按法人、医院或已发布组织单元定义范围，`HRP Access Scope Member` 按用户、
目标 DocType 和读取、新增、修改、提交、撤销、导出操作授权。普通用户无匹配范围时默认拒绝，多个
范围按并集合并，组织范围可安全展开同一版本的下级节点。

`CORE-001` 是只读解析 API，返回结构化过滤组和决策摘要，不接受任意字段、SQL、Python 或表达式。
解析始终先尊重 Frappe DocPerm，管理员全局范围也不能越过基础权限。医院和组织层级查询已接入统一
helper；完整模型、安全、迁移和回滚规则见 `architecture/access_scope.md` 与
`architecture/adr/ADR-0018-fail-closed-organization-access-scopes.md`。

## 业务规则与验证记录

`HRP Business Rule` 在真实单据及组织范围内执行字段、日期、金额、条件组合和明细约束，
使用受限 JSON 条件及冻结字段契约。`CORE-038` 至 `CORE-043` 提供修订配置、实时试算、
只追加证据及可见记录查询；“权限与授权”工作区提供配置表单和“业务规则验证”页面。

主数据申请提交已接入：阻断条件失败时回滚提交，提醒条件通过后保留同事务证据。
实时判断同时检查源单据、数据范围、字段及遮罩权限；历史记录只作证据，不能重放为当前
业务授权。完整语义、上限、接口与回滚方法见 `architecture/business_rules.md`。

## 非生产环境

```bash
cp .env.example .env
# 修改 .env 中两个密码
set -a && source .env && set +a
python scripts/environment_manager.py validate
python scripts/environment_manager.py plan development
python scripts/environment_manager.py provision development
```

仓库内置彼此隔离的 `development`、`test`、`demo` 三种配置档。它们分别使用
独立 Bench、Site、数据库和服务端口，默认只允许合成数据，并关闭外部集成和
邮件。测试和演示环境还关闭调度器；演示环境禁止测试接口。完整创建、验证、
审计、演示初始化和回滚方法见 `architecture/environments.md`。

## 测试数据工厂

`medops/config/test_data_scenarios.json` 登记版本化合成场景。生成只允许受管
`development`/`test` Site，并同时要求测试开关、仅合成数据、非公开访问和外部集成关闭。
场景由应用内静态 builder 实现，不接受任意 DocType/字段、SQL 或动态导入；写入复用
领域服务权限、savepoint、加密幂等和标准控制器。

`PLT-017` 只读接口可查询场景和安全策略，生成动作只通过
`medops.hrp_foundation.services.generate_test_data` 执行。工厂不生成 PII、不记录
种子或记录名，也不提供通用删除；持久测试数据通过 replaceable Site 重建清理。完整
模型、调用、验证和回滚方法见 `architecture/test_data_factory.md`。

## 性能基线与压测

`medops/config/performance_baselines.json` 版本化登记只读场景、资源硬上限和
smoke/baseline/load 阈值。`PLT-018` 只允许系统管理员读取注册表与环境许可；压测只能由
站点外的官方 k6 执行，不能从 Web 请求、后台任务或调度器启动。

执行器要求本地/目标注册表 SHA 一致，并仅放行受管 `development`/`test`、测试开关开启、
仅合成数据、非公开且外部集成关闭的 Site。固定请求数、VU 和时长均有上限，API Token
只从环境变量读取，报告不保存凭据、目标 URL 或响应正文。先查看无请求计划：

```bash
python scripts/performance_baseline.py \
  --base-url http://127.0.0.1:8200 \
  --profile smoke \
  --dry-run
```

初始场景覆盖认证到 MariaDB 的模块注册表只读链路；容量场景必须随对应业务 COD 增量交付。
完整阈值、执行、安全、制品和回滚方法见 `architecture/performance_baselines.md`。

## 软件供应链安全与SBOM

`medops/config/software_supply_chain.json` 固定 CycloneDX 1.7、扫描工具版本、
Linux 二进制 SHA-256、失败阈值和限期例外。`Security` CI 作业在完整 Git 历史上运行
Bandit、Gitleaks、pip-audit、npm audit 和 Grype，并生成包含 `medops`、三项锁定
上游以及 Python/Node 依赖的确定性 SBOM。

Frappe 站点只提供系统管理角色可读的 `PLT-019` 治理契约：

```text
GET /api/method/medops.api.v1.security.get_software_supply_chain_contract
```

站点、worker 和生产环境不能启动扫描，也不保存原始报告。CI 只上传最终 SBOM、脱敏
摘要和 SHA-256 清单；完整门禁、例外、运行和回滚规则见
`architecture/software_supply_chain.md`。

## Fixtures 治理

Fixture 只用于 `medops` 对标准 Frappe/ERPNext/HRMS 对象的受控配置扩展。
当前白名单为模块归属的 Custom Field、Property Setter，以及五个 HRP 核心角色
归属的 Custom DocPerm。导出前先运行：

```bash
python scripts/fixture_manager.py plan
python scripts/fixture_manager.py validate
```

实际导出只允许受管理的 development Site，经显式 `--yes` 后连续导出两次并
校验确定性；生产、测试和演示 Site 均拒绝。完整白名单、敏感数据限制、审计、
升级和回滚规则见 `architecture/fixtures.md`。

## 审批委托

`HRP Delegation` 将 COD-025 审批矩阵中的本人审批责任，在最长 366 天的受控有效期内
委托给具备目标单据写权限和同等组织范围的启用系统用户。创建与撤销只能通过
`CORE-008`、`CORE-030` 幂等服务执行；`CORE-031` 只允许管理员、审计员或委托双方读取。

委托锁定矩阵修订号和策略摘要。矩阵修订后旧委托自动失效；重叠委托、转委托链、循环、
自我委托和权限提升均默认拒绝。审批解析结果保留委托人、受托人和委托记录证据，但不直接
批准或修改业务单据。完整模型、安全边界、运维与回滚规则见 `architecture/delegation.md`。

## 不相容职责

`HRP Segregation Rule` 以封闭动作、真实单据 User 字段、冲突角色和组织范围定义 maker-checker
硬控制。`CORE-003` 在动作前同时验证调用方读权限、被校验用户 DocPerm 和 COD-024 组织范围；
任何责任人复用、冲突角色或规则漏配均返回 `allowed=false`。请求不能覆盖单据中的法人、医院、
组织或日期，规则也不能执行 SQL、Python、表达式或 Server Script。

`CORE-032` 通过幂等和乐观修订维护规则，`CORE-033` 供管理员与审计员只读查询。响应和审计只
保留规则、字段名、角色名、数量与摘要，不记录用户、组织、责任人值或单据名。完整设计、迁移和
回滚规则见 `architecture/segregation_of_duties.md` 与
`architecture/adr/ADR-0022-declarative-fail-closed-segregation-of-duties.md`。

## ADR 与变更治理

Git 中的 ADR 和结构化 COD 变更记录是工程治理的唯一权威。每个已完成任务
必须有 `changes/COD-XXX.json`；单应用边界、权限、上游基线、治理门禁和所有
破坏性变更必须关联 Accepted ADR。提交前运行：

```bash
python scripts/change_manager.py validate
python scripts/change_manager.py plan --base-ref origin/main --task COD-XXX
python scripts/change_manager.py check \
  --base-ref origin/main \
  --task COD-XXX \
  --correlation-id COD-XXX-local
```

CI 会对 Pull Request 的 base commit 和 `main` 推送的 before commit 执行相同
差异检查。Frappe 仅提供管理员只读、脱敏的治理状态 API，没有 HTTP 写入口。
ADR 状态机、风险门禁、审计与回滚规则见
`architecture/change_governance.md`。

## 统一异常与错误码

所有 `medops` 模块通过 `medops.services.errors` 抛出受控异常，不直接调用
`frappe.throw`。`medops/config/error_catalog.json` 定义稳定的
`IONE-CORE-xxxx` 机器码、HTTP 状态、重试语义和日志级别；英文源消息通过
标准 `medops/translations/zh.csv` 提供中文。错误响应包含安全的
`ione_error` 摘要、`X-Ione-Error-Code` 与随机 `X-Ione-Error-ID`，不包含
原因消息、堆栈、路径或业务载荷。

管理员可只读查询错误目录：

```text
GET /api/method/medops.api.v1.errors.get_error_catalog
```

错误目录没有 HTTP 写入口，也不创建 DocType 或站点数据。命名、兼容、安全、
开发用法与回滚规则见 `architecture/errors.md`。

## 审计上下文

应用使用锁定 Frappe v17 的请求和后台任务钩子，为每次执行生成服务端
`request_id`，并通过 `correlation_id` 关联 HTTP、服务和队列调用。所有响应
返回 `X-Correlation-ID` 与 `X-Request-ID`；受控错误体也包含这两个安全字段。
跨队列调用必须使用 `medops.services.audit_context.enqueue_with_audit`，自研
结构化审计必须使用 `emit_audit_event`，不得记录用户、患者、请求正文、路径、
站点或凭据。直接服务入口使用 `service_audit_scope` 隔离相邻调用，并在 HTTP
或后台任务中继承已有上下文。

已认证用户可只读查询当前请求上下文：

```text
GET /api/method/medops.api.v1.audit.get_audit_context
```

该能力不创建 DocType 或数据库记录。ID 规则、任务父子传播、权限、脱敏和回滚
见 `architecture/audit_context.md`。

## 领域服务与写入幂等

所有新的业务写入必须通过 `medops.services.domain_service.DomainService` 执行，并由所属
模块的 `services` 包暴露公共 facade。模板方法统一处理角色校验、领域校验、审计上下文、
数据库 savepoint、稳定异常和持久化幂等；服务不得调用 `frappe.db.commit()`，最终提交归
Frappe 请求或任务事务所有。

写 API 必须提供 `Idempotency-Key`。应用只保存键和请求的 SHA-256，不保存原始键或请求
正文；规范化响应使用 Site 密钥加密，并在重放前验证响应指纹。同键同参重放领域结果，
同键异参返回 `IONE-CORE-0007`。首个实现是：

```text
POST /api/method/medops.api.v1.modules.set_module_enabled
Idempotency-Key: deploy-20260729-budget-enable
```

内部 `HRP Service Idempotency` DocType 不加入 Workspace、Fixture 或全局搜索。服务开发、
事务、安全、迁移和回滚规则见 `architecture/domain_services.md` 与
`architecture/adr/ADR-0006-domain-service-and-durable-idempotency.md`。

## 不可变台账

新的 HRP 业务台账必须保留在所属模块中，使用结构化 DocType，并继承
`ImmutableLedgerDocument`、`AppendImmutableLedgerService` 和
`ReverseImmutableLedgerService`。公共基类统一强制服务专用追加、只读 DocPerm、
持久化幂等、`FOR UPDATE NOWAIT` 行锁、一次冲销以及等额反向校验；不提供任意
DocType 的通用写 API，也不创建万能 JSON 台账。

该能力不适用于 ERPNext `GL Entry`、`Stock Ledger Entry` 或 `Bin`。财务和库存过账
仍须调用 ERPNext 标准控制器。平台公共契约可由系统管理员只读查询：

```text
GET /api/method/medops.api.v1.ledgers.get_immutable_ledger_contract
```

模型字段、具体模块接入方法、并发语义、安全边界和回滚规则见
`architecture/immutable_ledgers.md` 与
`architecture/adr/ADR-0007-immutable-ledger-base.md`。

## Outbox / Inbox 事务消息

COD-013 提供领域事件和集成消息的公共基类。Outbox 在业务事务中原子追加待发布消息，
领取使用 MariaDB `FOR UPDATE NOWAIT`、有限租约和一次性令牌哈希；网络调用只能在领取
事务提交后执行。Inbox 使用 `consumer + event_id` 的确定性哈希名称持久化去重，并要求
消费者业务写入与 `Processed` 状态在同一事务完成。

所有具体 MessageBox DocType 只授予读取权限，插入和状态迁移只能经继承公共基类的领域
服务执行。服务复用加密持久化幂等、关联 ID、savepoint 和脱敏审计，不调用
`frappe.db.commit()`，也不记录事件载荷、结果或处理令牌。

公共只读契约：

```text
GET /api/method/medops.api.v1.messages.get_transactional_message_contract
```

本基线不创建通用生产消息表，也不提前实现外部系统网络投递。详细设计见
`architecture/transactional_messages.md` 与
`architecture/adr/ADR-0008-transactional-outbox-inbox.md`。

安装到已有非生产 Bench：

```bash
BENCH_DIR=/path/to/frappe-bench SITE_NAME=hrp.localhost \
  ./scripts/install_into_existing_bench.sh
```

只验证已有 Bench，不安装应用：

```bash
python scripts/version_lock.py --bench /path/to/frappe-bench
```

查看锁定提交是否仍是官方 `develop` 当前头部（只用于基线刷新评估，不是日常
部署门禁）：

```bash
python scripts/version_lock.py --verify-remote-heads
```

新增代码模块（应用安装进 Bench 后）：

```bash
cd /path/to/frappe-bench
bench medops-create-module   --name "HRP Medical Insurance"   --group "Finance"   --label-cn "医保运营"   --description "医保目录、结算、拒付和申诉管理"   --yes
bench --site hrp.localhost migrate
```

也可在开发包根目录运行 `python3 scripts/create_module.py ...`。

注意：模块是版本控制中的代码结构，不允许普通业务用户在生产界面任意创建。应用内的 `HRP Module Setting` 只控制模块启用状态，不生成 Python 源码。

## 仓库契约

提交前必须运行：

```bash
python scripts/repository_contract.py
python scripts/change_manager.py validate
python scripts/validate_package.py
python scripts/checksums.py
python -m unittest discover -s tests -p "test_*.py"
```

只允许仓库根目录的 `medops` 一个自研 App。禁止将 Frappe、ERPNext、Frappe HR
源码复制到本仓库，禁止新建 `medops_*` 业务 App。

## 开发质量工具

安装锁定的开发工具：

```bash
python -m pip install -e ".[dev]"
npm ci
```

运行统一质量门禁：

```bash
python scripts/quality.py
```

该命令依次执行仓库契约、包校验、Python 编译、Ruff lint/format、Bandit、
仓库单元测试、校验和、Pyright、ESLint 和 Prettier。工具缺失或任一步失败都会返回
非零状态，不允许静默跳过。只检查 Python 或 Node 工具可分别使用
`--mode python`、`--mode node`。详细规则见
`architecture/quality_tooling.md`。

## 持续集成

流程基础已加入服务时限与超时升级：通过受控策略为真实申请建立计时周期，支持暂停恢复、响应确认、超时待办及结束清理。策略和日历按周期冻结，复用 ERPNext 日历及 Frappe 标准待办、站内通知；操作和运行边界见 [服务时限说明](architecture/sla.md)。

Pull Request、`main` 推送和人工运行都会触发 `.github/workflows/ci.yml`：

- `Quality` 执行仓库契约、格式、静态分析、类型、单元和前端质量门禁；
- `Security` 固定并校验扫描工具，扫描完整 Git 历史和依赖，生成 CycloneDX 1.7
  SBOM，并只上传脱敏安全证据；
- `Integration` 用锁定的 Frappe、ERPNext、Frappe HR 提交创建临时 Bench，
  迁移新站点并运行全部 `medops` 集成测试；
- `Required` 只在 Quality、Security 和 Integration 都成功时通过，并由分支保护要求
  `Required` 检查上下文（GitHub 页面显示为 `CI / Required`）。

CI 使用一次性测试密码且不连接 Press 或生产环境。完整设计、安全边界和本地
复现命令见 `architecture/ci_pipeline.md`。

## 核心工作台与管理报表

应用首页预览个人待办、待复核申请、质量问题和服务时限，并提供八张核心报表。医院使用的标准主数据须显式登记；定时质量巡检仅处理有效登记，升级时需先确认登记范围。组织差异、主数据时效、重复值、编码映射、真实质量历史、委托和职责初筛均重查当前来源与导出权限。详见 `architecture/core_operational_reports.md`；医院完整业务与容量验收仍以 `architecture/production_readiness.md` 为准。

## 预算编制（开发验证已交付）

预算编制页面提供分类和资金来源维护、预算版本、科室明细、精确期间分摊、撤回、冻结与复制。冻结保留编制事实，不完成财务审批或形成可执行额度。复用 ERPNext 原生年度、科目、成本中心、项目、币种和日历；用户仍须获得明确的范围及原生来源读取权限。接口、迁移和回滚见 [预算编制说明](architecture/budget_planning.md)。

COD-032已交付精确科室申报、完整来源汇总及持久化人工审批，并经备份验证部署到医疗开发/测试站点。380项纯逻辑、360项全量Frappe、实际页面与七组并发通过；不自动授予用户权限，批准仍不产生可用预算或原生过账。实现、迁移和回退见[申报审批说明](architecture/budget_approval.md)及[交付记录](architecture/releases/COD-032.json)。后续继续COD-033至035预算台账与执行闭环，整体医院生产验收尚未完成。

COD-033开发分支已实现精确下达/完整冲销、独立人工确认、提交后派发与恢复调度、完整账簿/单笔/XLSX及预算操作台。86ba9b0完整456项本地质量、1449提交字节和6项定向原生通过；完整446出现2项旧HTTP幂等测试身份冲突，9项数据/环境保护检查通过。测试身份修正及重复运行复验继续推进；页面补验、性能、备份恢复、保护CI及发布门槛尚未全部完成。公开站点保持COD-032。详见[预算台账开发边界](architecture/budget_ledger.md)。

后续真实并发调度发现两个相同队列项；隔离标准worker仍只产生一批两条精确分录，原有默认386项未消费，long恢复为空。应用增加站点/任务短锁及锁占用后保留授权、扫描恢复的原生测试，库存439/436，等待复验。操作台详情和50条目录已实际渲染，但目录查询观察到数分钟等待，性能及其余页面流程须继续验证；不登记Done或发布。

4f16f78完整439质量、派发9项与真实双进程调度/RQ复验通过：两个提交回调重叠，只进入一个任务并生成一批两条分录，默认386项未消费，long为空且worker退出。目录252候选剖析耗时254.195秒，重复完整来源证明为主要开销。新增仅限自有只读连接/执行人/单图阶段的复用，审批键绑定全部传入字段和显式身份，首次与最终核验不共享图，锁定写入不复用；页面增加读取状态和重复查询保护。新增2纯逻辑、1原生身份/篡改绑定用例，库存441/437；新优化、51项联合原生及页面仍待验收，不宣称达到医院容量。

0898b5d完整441质量通过，同一252候选的完整目录剖析从254.195秒降到53.786秒，仍需继续提速。申报/计划/版本权限加入同样的只读单阶段复用，键使用完整传入字段的SHA256，避免保留私有快照的重复序列化副本；原生篡改/身份用例扩展到完整汇总申报。新优化、51项联合原生和实际页面仍待验证。
