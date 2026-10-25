# HRP Foundation

**领域组：** Core

**中文名称：** 基础平台

系统设置、模块注册、特性开关、公共字典、编号、规则与通用能力。

全局配置由标准 Single DocType `HRP System Settings` 承载，只允许
`System Manager` 和 `HRP System Manager` 管理。发布通道、严格数据域和 AI
人工确认属于固定代码策略；可编辑配置通过版本校验、行锁、幂等领域服务和脱敏审计
更新，不允许任意 JSON。规则见 `architecture/system_settings.md`。

工程 ADR 和变更记录以 Git 为唯一权威。基础平台通过
`medops.services.change_governance` 提供管理员只读的脱敏治理状态；没有
站点内写入、审批或状态转换入口。规则见
`architecture/change_governance.md`。

应用异常统一通过 `medops.services.errors` 抛出，使用稳定
`IONE-CORE-xxxx` 代码、标准 HTTP 状态、中英文消息、安全响应头和脱敏审计。
错误目录由 Git 管理，不建立站点内可编辑模型。规则见
`architecture/errors.md`。

测试数据工厂通过源码登记的场景和静态 builder 生成确定性合成数据，只允许受管
development/test Site。生成复用领域服务的角色、幂等、savepoint 和脱敏审计；
`PLT-017` 只公开只读契约。规则见 `architecture/test_data_factory.md`。
