# HRP Workflow Authorization

**领域组：** Core

**中文名称：** 流程与授权

审批矩阵、金额权限、委托、SLA、不相容岗位和数据权限。

COD-024 已实现统一访问范围，COD-025 与 COD-026 已实现确定性审批矩阵和修订锁定委托。COD-027
新增声明式不相容职责规则：从真实单据派生组织和业务日期，按封闭动作、User 字段及冲突角色校验，
冲突或规则漏配均默认拒绝。完整设计见 `architecture/access_scope.md`、
`architecture/approval_matrix.md`、`architecture/delegation.md` 与
`architecture/segregation_of_duties.md`。
