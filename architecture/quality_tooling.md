# 代码质量工具基线

## 目标

`medops` 使用一套固定版本、跨平台、缺失即失败的本地质量工具。COD-004
只建立工具和可执行门禁，不提前创建 COD-005 的 GitHub Actions 或 Press CI。

## 固定版本

| 工具 | 版本 | 配置 |
| --- | --- | --- |
| Ruff | `0.15.9` | `pyproject.toml` |
| Bandit | `1.9.4` | `pyproject.toml` |
| pip-audit | `2.10.1` | `pyproject.toml` |
| Pyright | `1.1.411` | `pyrightconfig.json` |
| ESLint | `10.8.0` | `eslint.config.mjs` |
| Prettier | `3.9.6` | `.prettierrc.json` |
| TypeScript | `6.0.3` | `package.json` |
| typescript-eslint | `8.65.0` | `eslint.config.mjs` |
| eslint-plugin-vue | `10.10.0` | `eslint.config.mjs` |

Python 工具通过 `project.optional-dependencies.dev` 精确固定；Node 工具在
`package.json` 使用无 `^`/`~` 的精确版本，并由 `package-lock.json` 锁定
完整依赖树。更新任一版本必须同时更新配置、锁文件、测试证据和本文档。

## 安装

```bash
python -m pip install -e ".[dev]"
npm ci
```

Node.js 最低版本为 `20.19.0`。禁止用 `npm install --no-package-lock` 或手工
修改 `node_modules` 代替锁文件更新。

## 统一入口

```bash
python scripts/quality.py
python scripts/quality.py --mode python
python scripts/quality.py --mode node
```

Linux/Bench 也可执行：

```bash
./scripts/quality.sh
```

完整模式按顺序执行：

1. 单应用仓库契约与打包校验；
2. Python 编译、Ruff lint、Ruff format；
3. Bandit 中等严重度与置信度静态安全扫描；
4. 仓库单元测试与 `SHA256SUMS.txt` 校验；
5. Pyright 类型检查；
6. ESLint 的 JavaScript、TypeScript、Vue 检查；
7. Prettier 的前端源码、Frappe JSON 和工具配置检查。

任何配置文件缺失、npm 不可用、命令返回非零或锁文件漂移都会立即失败。
`NPM_BIN` 可用于显式指定 npm 路径，但无效路径同样失败。

## Python 规则

- Ruff 目标为 Python 3.10，与应用最低运行时一致。
- 检查 `medops`、`scripts`、`tests`，排除上游 `apps`、构建产物和依赖目录。
- 使用 F/E/W/I/UP/B/RUF 规则；只保留行宽、Tab 缩进和中文歧义字符三项有
  明确原因的全局忽略。
- 三个直接执行脚本因启动时注入仓库根目录，仅按文件忽略 E402。
- Pyright 使用 `basic` 模式覆盖相同目录。开发包本地未安装 Frappe 时忽略
  缺失的上游导入；统一质量入口会把当前 `sys.executable` 传给 Pyright，在
  隔离 Bench 中解析并使用真实锁定依赖再次执行。

## 前端规则

- ESLint 使用 flat config，覆盖 JavaScript、TypeScript 和 Vue。
- 浏览器代码只开放浏览器标准全局及 Frappe 公共全局。
- TypeScript/Vue 使用 typescript-eslint；Vue 使用 `vue-eslint-parser` 的
  flat recommended 规则。
- Prettier 只负责前端源码、Frappe JSON 和质量工具配置，不批量改写业务
  CSV、架构 YAML、Markdown 或锁定版本证据。
- ESLint 只负责语义规则，格式冲突由 `eslint-config-prettier` 关闭。

## 适用性与安全

本任务不创建 DocType、数据库写服务、业务 API、权限或迁移，不涉及业务审计
和重试幂等。质量执行器是只读且可重复运行的；自动化测试验证缺少 npm、配置
缺失、依赖未固定、命令失败、执行顺序和重复执行不修改配置等异常路径。
