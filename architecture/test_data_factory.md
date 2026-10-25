# 测试数据工厂

## 目标

COD-014 为 `medops` 提供统一、确定性、可审计的合成测试数据入口。工厂服务于集成测试、
开发验证和后续性能场景，不复制生产数据，不接收任意 DocType/字段，也不建立可在 Desk
中编辑的生产模型。

机器权威是 `medops/config/test_data_scenarios.json`。纯 Python 模型负责严格解析、依赖
排序、种子规范化和确定性标识；`medops.services.test_data_factory` 负责环境、权限、
幂等、事务和实际控制器调用。业务模块新增场景时必须把 builder 作为源码实现并登记，
不得在 JSON 中填写 Python 路径、SQL 或表达式。

## 场景模型

每个场景包含：

- 稳定 `scenario_id` 和显式版本；
- 允许的受管环境；
- 执行所需角色；
- `contains_personal_data=false` 的强制声明；
- 有界步骤及其依赖关系；
- 由应用内静态映射解析的 builder 标识。

每个 `scenario_id` 只登记一个当前版本，升级时直接提升该场景的显式版本。步骤按稳定拓扑序
执行。未知依赖、环、重复场景 ID、未登记 builder、生产环境、个人数据声明
或额外字段都会使注册表加载失败。输入种子长度为 8～64，只允许受控 ASCII；数据集和记录
标识使用 SHA-256 派生，审计不保存原始种子、数据集 ID 或记录名。

首个 `platform-smoke` 场景按依赖顺序创建两个禁用的 `HRP Feature Flag` 合成标记。它只
验证平台写入、重放和回滚能力，不启用功能，也不生成患者、员工、供应商、银行账户或
其他个人/敏感数据。后续领域任务应增加自己的最小主数据场景，不把交易和台账样例塞入
平台场景。

## 安全边界

生成前按固定顺序执行：

1. 要求 `System Manager` 或 `HRP System Manager`；
2. 验证场景和种子；
3. 读取并严格校验 COD-006 环境配置；
4. 仅允许 `development` 或 `test`，且必须同时满足 `allow_tests=true`、
   `synthetic_data_only=true`、非公开访问和外部集成关闭；
5. 建立 COD-011 的 savepoint 和持久化幂等预留；
6. 经对应 DocType/ERPNext 标准控制器写入；
7. 保存加密响应快照并输出脱敏审计。

未受管站点、`demo`、生产站点和配置漂移都会在任何数据写入和幂等预留前失败。服务不调用
`commit`，不直接写 `GL Entry`、`Stock Ledger Entry`、`Bin`，不修改已提交单据，也不
开放 HTTP 写方法。只读 `PLT-017` 用于查询场景和安全策略。

## 使用

先确认环境：

```bash
bench --site hrp-test.localhost execute \
  medops.api.v1.test_data.get_test_data_factory_contract
```

生成动作只通过公开服务 facade 执行，并提供唯一幂等键：

```bash
bench --site hrp-test.localhost execute \
  medops.hrp_foundation.services.generate_test_data \
  --kwargs '{
    "scenario_id": "platform-smoke",
    "seed": "COD-014-manual-0001",
    "idempotency_key": "COD-014-manual-generation-0001",
    "correlation_id": "COD-014-manual-generation-0001"
  }'
```

同一幂等键和请求重放加密结果，不重复执行；同一场景和种子使用新幂等键时，builder 校验
既有记录并返回 `changed=false`。同键异参返回幂等冲突。

## 清理与回滚

工厂故意不提供逐表删除 API。合成记录可能被后续场景引用，通用级联删除会绕过领域规则。
测试由 Frappe 测试事务回滚；持久开发/测试数据按 COD-006 的
`reset_policy=replaceable` 重建整个隔离 Site。禁止把非生产数据库恢复到生产。

COD-014 不新增生产 DocType、Fixture、Patch 或表。回滚应用提交即可；已生成的非生产
数据通过隔离 Site 重建清除，不在生产站点执行清理脚本。

## 验证

仓库测试覆盖注册表、依赖排序、确定性标识、危险配置和隐式默认拒绝。锁定 Bench 集成
测试覆盖真实 DocType 控制器、角色先行、环境先行、builder 映射漂移、持久化幂等、
同场景异键幂等、部分失败回滚、冲突保护、审计脱敏以及 `PLT-017` 的真实 HTTP 权限。
