# 部署蓝图

生产建议采用 Frappe v17 develop 高可用拓扑：Ingress/Nginx → 多 Web/Gunicorn 实例；独立短队列、默认队列和长队列 worker；独立 scheduler；Redis cache/queue/socketio；MariaDB 主从或受管高可用；S3/MinIO；集中日志、指标和追踪。

核心交易建议一个医疗集团按数据隔离要求选择：
- 单 Site、多法人/多院区：共享主数据与集团分析最便利，需严格行级权限和容量治理；
- 多 Site、集团数据平台：隔离更强，通过主数据中心、事件和数据仓库汇总；
- 不建议在第一期把每个领域拆成独立微服务。Frappe App 是代码边界，Site 内事务是核心一致性边界。

环境至少包括 local、CI、dev、SIT、UAT、preprod、prod、DR。COD-006 的
development、test、demo 非生产基线见 `architecture/environments.md`；三者
不得承载生产数据。配置和秘密不得进入 Git；使用 Vault/KMS/Secret Manager。
生产发布采用不可变镜像、数据库备份、迁移预演、滚动发布和自动回滚闸门。

## 上游版本推广闸门

1. `resolved_versions.lock.json` 是 Frappe、ERPNext、Frappe HR 的唯一版本权威。
2. 每个锁定 SHA 必须来自对应官方仓库的 `develop`，不能使用本地 snapshot
   commit、浮动分支头或未经审计的 fork 提交。
3. Press App Source 可以指向公司 fork，但用于发布的分支必须精确包含锁定的
   官方 commit；自研修改必须留在 `medops`，不得叠加到三个上游仓库。
4. 新组合先在隔离 Bench 完成 `bench new-site`、三个上游 App 安装、
   `medops` 安装、`migrate` 和应用测试。
5. 生产更新前执行完整数据库与文件备份，创建新的 Deploy Candidate，并先
   检查候选中三个 App 的 hash 与锁文件一致。
6. 站点切换成功并完成健康检查后，才能设置：

   ```bash
   bench --site <site> set-config medops_enforce_upstream_lock 1
   ```

   当前运行提交未对齐时不得提前启用，否则 `after_migrate` 会阻止迁移。
7. 回滚使用更新前备份和上一 Deploy Candidate；禁止直接改数据库伪造版本状态。
