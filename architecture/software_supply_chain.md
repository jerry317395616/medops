# 软件供应链安全与SBOM

## 边界

COD-016 只在 `medops` 内建立软件供应链契约。安全扫描和 SBOM 生成由站点外的
CI 或发布进程执行，不在 Frappe Web、worker、scheduler 或 Press 生产 Site 中运行。

不新增 DocType、数据库表、Fixture、Patch、Workspace 或第二个自研 App，也不修改
Frappe、ERPNext、Frappe HR 源码。

## 权威来源

| 来源 | 责任 |
| --- | --- |
| `medops/config/software_supply_chain.json` | SBOM、执行边界、工具、门禁和例外 |
| `version_lock.json` | 三个上游仓库、版本和锁定提交 |
| `pyproject.toml` | Python 运行时与开发工具依赖 |
| `package-lock.json` | Node 开发依赖图 |
| `.gitleaksignore` | 已批准历史发现的精确指纹 |
| `.github/workflows/ci.yml` | 固定资产下载、校验、扫描和制品保留 |

所有配置使用严格键集合。未知键、缺失键、重复组件、不安全路径、未知依赖边和过期
例外都会失败关闭。

## SBOM构成

最终制品是 CycloneDX JSON 1.7：

- 根组件：`medops` 当前应用版本和本次源码提交；
- 必需组件：锁定的 `frappe`、`erpnext`、`hrms`；
- Python 组件：由 pip-audit 解析项目元数据；
- Node 组件：由 npm 原生 `npm sbom` 生成的开发依赖图；
- 依赖边：根组件连接所有直接依赖，上游关系按锁定架构显式建模。

序列号使用源码提交和规范化组件图生成，组件与依赖排序固定。相同输入生成相同
SBOM。制品拒绝本地绝对路径、`file://` 引用和未知依赖引用。

## 安全门禁

| 门禁 | 失败条件 |
| --- | --- |
| Bandit | 中等及以上严重度和置信度发现数大于 0 |
| Gitleaks | 未精确批准的历史秘密发现数大于 0 |
| pip-audit | Python 漏洞数大于 0 |
| npm audit | high 或 critical 漏洞大于 0 |
| Grype | High 或 Critical 漏洞大于 0 |
| Grype DB | 数据库不可识别、时间在未来或超过 5 天 |
| License | 拒绝许可证命中数大于 0 |
| Exception | 任一治理例外已到期 |

被拒绝的许可证为 BUSL-1.1、Commons-Clause、
PolyForm-Noncommercial-1.0.0 和 SSPL-1.0。

## 工具身份

Python 工具通过 `pyproject.toml` 固定：

- Bandit 1.9.4；
- pip-audit 2.10.1。

CI 固定 npm 11.17.0。Gitleaks、Grype 和 CycloneDX CLI 同时固定版本、Linux
资产名和 SHA-256。任何版本或摘要不一致都在扫描前失败。

## 站点服务

`PLT-019`：

```text
GET /api/method/medops.api.v1.security.get_software_supply_chain_contract
```

仅 `System Manager` 或 `HRP System Manager` 可读取。服务返回策略 SHA、SBOM
格式、固定工具、门禁和例外摘要，并明确：

- `scan_available_from_site=false`；
- `http_write_enabled=false`；
- `site_execution_enabled=false`；
- `production_execution_enabled=false`。

权限在读取策略前校验。重复读取不创建 Version、Comment、Error Log 或幂等记录。
审计仅记录策略 SHA、工具数、例外数和仅限外部执行标志。

## 本地运行

先准备与策略完全一致的 Gitleaks、Grype 和 CycloneDX CLI，然后运行：

```bash
python scripts/security_supply_chain.py plan
python scripts/security_supply_chain.py run \
  --source-commit "$(git rev-parse HEAD)" \
  --npm-bin npm \
  --gitleaks-bin /path/to/gitleaks \
  --grype-bin /path/to/grype \
  --cyclonedx-bin /path/to/cyclonedx
```

输出限制在 `.artifacts/security`。`raw/` 仅用于本地判定，不上传；可分发制品只有
最终 SBOM、脱敏摘要和 SHA-256 清单。

## CI与制品

`Security` 依赖 `Quality`，与 `Integration` 并行。`Required` 同时聚合三者。
工作流权限保持 `contents: read`，不引用仓库 Secret。

安全证据保留 30 天。摘要只包含计数、工具版本、策略 SHA、源码提交、SBOM SHA 和
数据库时间，不包含代码片段、文件路径、Token、用户名、站点名或原始发现。

## 例外管理

例外必须同时具备：

- 精确种类、规则 ID 和包或仓库相对路径；
- ISO 日期到期时间；
- 真实原因和批准人；
- 对 Gitleaks，`.gitleaksignore` 中完整的单一指纹。

禁止使用全局正则、目录级排除或永久例外。修改、续期或删除例外必须通过新的 COD。

## 迁移与回滚

无数据库迁移。应用升级只同步 Python/API 元数据。回滚应用提交和 CI 配置即可，
删除 `.artifacts/security` 或 CI 制品不会影响 Site 数据。
