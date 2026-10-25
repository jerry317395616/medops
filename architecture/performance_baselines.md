# 性能基线与压测

## 目标

COD-015 为 `medops` 建立可重复、资源有界、默认拒绝生产的性能基线。性能预算、场景、
执行档和响应契约全部进入 Git；真实请求由站点外的 k6 `2.1.0` 进程发起，Frappe 只提供受权限保护
的只读契约。Web 请求、后台任务和调度器均不能启动压测。

机器权威是 `medops/config/performance_baselines.json`。纯 Python 模型严格解析资源上限、
场景和阈值，`medops.services.performance_baseline` 负责角色、环境状态和脱敏审计，
`scripts/performance_baseline.py` 负责安全目标校验、k6 进程编排及独立结果复核。

## 初始基线

首个 `platform-module-registry-read` 场景请求
`medops.api.v1.modules.list_modules`。它覆盖认证、Frappe 路由、角色会话、MariaDB 查询、
模块注册表合并和 JSON 序列化，但不读取个人信息，也不写数据库。

| 执行档 | 虚拟用户 | 固定请求数 | 最长场景时间 | 允许错误率 | 最低检查率 | p95 | p99 | 最低吞吐 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| smoke | 1 | 5 | 15 秒 | 0% | 100% | 1000 ms | 1500 ms | 0.2 req/s |
| baseline | 10 | 200 | 60 秒 | 1% | 99% | 750 ms | 1500 ms | 3 req/s |
| load | 25 | 1000 | 180 秒 | 1% | 99% | 1000 ms | 2000 ms | 5 req/s |

全局硬上限为 50 VU、5000 次请求和 300 秒。注册表会拒绝超过全局上限、p95 高于 p99、
最低吞吐不可能在限定时长内达到、布尔值伪装整数、重复身份、非 GET、任意 URL、个人数据
或写场景。新增场景必须增加静态响应验证器、单元测试和变更记录。

## 安全门禁

k6 在发送场景请求前先读取：

```text
GET /api/method/medops.api.v1.performance.get_performance_baseline_contract
```

必须同时满足：

1. 调用身份具有 `System Manager` 或 `HRP System Manager`；
2. 本地注册表 SHA-256 与目标站点一致；
3. Site 是 COD-006 管理的 `development` 或 `test`；
4. `allow_tests=true` 且仅允许合成数据；
5. Site 非公开并关闭外部集成；
6. 显式设置 `IONE_PERF_CONFIRM=NON_PRODUCTION_LOAD_TEST`；
7. HTTPS 目标不含用户名、密码、路径、查询或片段；仅 loopback 开发地址允许 HTTP。

任何检查失败都在场景请求前终止。配置没有 production、demo 或任意地址场景；HTTP
契约没有 POST。API Token 只从进程环境读取，不进入命令行、Git、结果或审计。结果也不
保存目标 URL。

## 执行

先安装契约锁定的官方 k6 `2.1.0`，校验官方发布 SHA-256，并在受管理测试站点为专用系统管理员生成短期 API Key/Secret。查看计划
不需要凭据，也不会启动 k6：

```bash
python scripts/performance_baseline.py \
  --base-url http://127.0.0.1:8200 \
  --scenario platform-module-registry-read \
  --profile smoke \
  --dry-run
```

实际执行：

```bash
export IONE_PERF_CONFIRM=NON_PRODUCTION_LOAD_TEST
export IONE_PERF_API_KEY='temporary-test-key'
export IONE_PERF_API_SECRET='temporary-test-secret'
python scripts/performance_baseline.py \
  --base-url http://127.0.0.1:8200 \
  --scenario platform-module-registry-read \
  --profile smoke \
  --output .artifacts/performance/smoke.json
```

Windows PowerShell 使用 `$env:IONE_PERF_CONFIRM=...` 等价设置。先运行 smoke，再运行
baseline；只有 baseline 稳定通过且测试环境容量明确时才运行 load。禁止把
`manager.myyr.top`、Press 生产 Site、公开 demo 或第三方地址作为目标。

`--output` 只能写入仓库的 `.artifacts/performance/*.json`，拒绝路径逃逸、非 JSON
文件和任意源码覆盖。k6 子进程不继承调用进程中的任意变量；执行器只白名单传递操作系统、
代理和证书变量，再显式注入本次运行所需的 `IONE_PERF_*` 值。因此外部
`K6_HTTP_DEBUG`、`K6_OUT`、`K6_INSECURE_SKIP_TLS_VERIFY` 或无关 Secret 不能改变
受控执行行为或进入子进程。

执行器把注册表 SHA、场景版本和资源上限传给 k6。k6 阈值失败返回非零；Python 随后严格
解析脱敏汇总并独立检查固定请求数、错误率、检查率、p95、p99 和吞吐率。最终报告只含
随机运行 ID、场景身份、阈值和聚合指标。

## CI 与回归

常规 CI 验证注册表、执行器、响应契约、结果评估器和真实 HTTP 权限，不在共享 GitHub
Runner 上对外发起压测。容量基线应在隔离、生产等价的 test 环境执行，报告作为发布证据
存入受控制品系统；`.artifacts/` 被 Git 忽略，也不进入 `SHA256SUMS.txt` 仓库清单。

`design/tests.csv` 中 TST-0133 至 TST-0147 是后续领域并发、容量、故障注入和安全场景，
不会在 COD-015 伪造为已完成。对应领域 DocType 和服务交付后，使用相同注册表增加有界
场景，并保留数据一致性断言。

## 迁移、回滚与故障处理

本任务不新增 DocType、表、Fixture、Workspace 或 Patch，不修改标准单据，也不直接写
`GL Entry`、`Stock Ledger Entry` 或 `Bin`。部署只增加源码配置、只读 API 和脚本；常规
`bench migrate` 不产生数据迁移。

若 smoke 失败，先停止测试并检查目标环境门禁、错误率和 Error Log；不得通过提高阈值掩盖
回归。回滚应用提交即可移除契约和脚本。性能结果是外部制品，不恢复到 Site 数据库；临时
API Secret 执行后立即吊销。
