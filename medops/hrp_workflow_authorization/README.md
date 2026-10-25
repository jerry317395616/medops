# HRP Workflow Authorization

**领域组：** Core

**中文名称：** 流程与授权

审批矩阵、金额权限、委托、SLA、不相容岗位和数据权限。

COD-024 已实现统一访问范围：管理员按法人、医院或已发布组织单元配置用户、目标 DocType 和操作，
业务查询通过默认拒绝的结构化过滤 helper 与 `CORE-001` API 解析。审批矩阵、职责分离和委托在后续
任务中实现。完整设计见 `architecture/access_scope.md`。
