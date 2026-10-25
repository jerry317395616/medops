# HRP Organization

**领域组：** Core

**中文名称：** 组织管理

医疗集团、法人、医院、院区、科室、护理单元、病区和岗位组织模型。

## 当前实现

COD-018 交付医院、组织版本和组织单元三个标准 DocType。每个版本保存一所医院的完整
Nested Set 组织树，整树通过领域服务原子替换，发布后不可变，并支持按生效日期查询。

组织版本和节点在 Desk 中只读；`System Manager`、`HRP System Manager` 和
`HRP Data Steward` 通过受控 API 管理。

COD-019 新增只读 `HRP Organization Mapping`，把发布版本内的组织单元映射到同一法人
现有的 ERPNext `Department` 和 `Cost Center`。维护服务校验版本状态、目标启停、版本内
唯一性和两棵 Nested Set 树的父子关系；不会创建或修改标准记录。三个管理角色可维护，
`HRP Integration User` 仅可通过 `CORE-015` 查询。
