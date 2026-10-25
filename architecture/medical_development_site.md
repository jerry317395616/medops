# 医疗生态开发站点

## 项目与环境

现有 HRP 项目正式更名为医衡 MedOps，仓库为
<https://github.com/jerry317395616/medops>。本应用继续承担医院运营与资源管理；
完整医疗领域通过后续经过兼容验证的生态应用扩展。

| 项目           | 配置                                       |
| -------------- | ------------------------------------------ |
| 公网入口       | `https://medical.myyr.top`                 |
| 内部开发站     | `medical-dev.localhost`                    |
| 内部测试站     | `test-medops.localhost`，不经公网路由      |
| 专用 Bench     | `/home/zyd/frappe/medical-env/bench`       |
| 数据服务       | Docker Compose 项目 `medical-dev`          |
| 数据库端口     | `127.0.0.1:13350`                          |
| 缓存与队列端口 | `127.0.0.1:17350`、`127.0.0.1:17150`       |
| Web 与代理端口 | `127.0.0.1:17850`、`127.0.0.1:17086`       |
| 实时服务       | 专用 Bench 内 `run/socketio.sock`          |
| 应用进程       | `frappe-medical-*.service`                 |
| 隧道           | `medical-dev-20261025`，独立于既有站点隧道 |

新站安装 Frappe、ERPNext、HRMS、MedOps。框架应用只从
`jerry317395616` 的 fork 获取，以 `resolved_versions.lock.json` 的提交为准。
新增生态应用应先验证个人 fork、版本兼容、许可证及数据边界，再安装到本环境。
此次源地址补丁不改变 Frappe、ERPNext、HRMS 的产品业务功能。

## 运行边界

开发与测试站遵循 `scripts/environment_manager.py` 的非生产环境规则，内部名称
以 `.localhost` 结尾，使用合成数据，关闭外部集成和邮件队列。开发站需要正式
机构资料初始化后才能演示完整 ERPNext/HRMS 业务；不预设真实医院或患者数据。
目前已有模块登记和基础能力不代表 36 个业务模块全部完成。

独立 nginx 仅接受 `medical.myyr.top`，将请求固定映射到 `medical-dev.localhost`。
测试站无域名路由。私有文件通过 Frappe 权限校验后的内部重定向读取。
数据库、Redis 与 Web 均使用回环地址，不更改其他 Bench 的配置和进程。

## 验证与维护

安装后仅对新 Bench 执行迁移、资源构建与服务操作。集成测试使用独立测试站：

```bash
cd /home/zyd/frappe/medical-env/bench
bench --site test-medops.localhost run-tests --app medops
bench --site medical-dev.localhost list-apps
env/bin/python apps/medops/scripts/version_lock.py --bench "$PWD"
curl --fail -H 'Host: medical.myyr.top' http://127.0.0.1:17086/api/method/ping
```

命令使用 `zyd` 账号，PATH 包含 `/home/zyd/.local/opt/node24/bin` 和
`/home/zyd/.local/bin`。部署证据保存在
`/home/zyd/frappe/medical-deployment-evidence/`，包含安装、配置、测试、HTTP 验证
及既有站点部署前后服务快照；不能仅凭本说明认定检查已通过。

管理员登录信息只保存在服务器 `medical-env/secrets/medical-login.txt`。
数据库密钥与站点配置仅保存在服务器私有文件中，不写入仓库、PR 或日志。

升级先在测试站验证，完成新站备份后再迁移开发站，仅重启本项目的专用服务。
不要使用全局 `bench restart`。撤销部署时先停止 `frappe-medical-*` 专用服务，
保留 Compose 数据卷和备份，按已验证版本恢复新站。禁止卸载或迁移既有站点的
`ione_hrp` 应用；本次改名仅用于全新站点。
