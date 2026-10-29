# 人工预占操作接口契约

任务 COD-035，架构决策 ADR-0039。当前为默认关闭的开发候选；本文件不构成生产启用、医院验收或服务器维护结束的证据。原生页面候选已实现，实际 Desk 浏览器及医院验收未完成。

## 边界与开关

所有方法使用 `POST /api/method/medops.api.v1.finance.<方法>`，JSON 请求体包含 `payload`，可选 `correlation_id`。未知参数拒绝；不得传入服务类、任意字段或 SQL 条件。命令必须携带稳定的 `Idempotency-Key` 请求头。相同命令重试保留原键；改变命令须先核实上一笔结果。

必须是当前已认证 System User 的网页 cookie 会话，且具有 `System Manager`、`HRP System Manager` 或 `HRP Budget Manager` 之一；仍逐次检查来源、字段、组织与财务数据范围。拒绝 GET、访客、带 Authorization 头的调用、后台任务和缺少明确 `acknowledge: true` 的财务命令。沿用原生 Frappe 认证/CSRF，标准证据 DocType 权限继续关闭，不授予用户任何新角色或范围。

| 方法 | 总开关之外仍需严格为真的配置 | 影响 |
| --- | --- | --- |
| `reservation.source` / `reservation.prepare` / `reservation.confirm` | `medops_reservation_confirmation_enabled`；这组原有方法不受新总开关控制 | 来源快照 / 保存独立确认 / 保存原始决定，均不预占额度 |
| `reservation_operations.post` | `medops_internal_reservation_write_enabled` | 首次预占 |
| `reservation_operations.renew` | `medops_internal_reservation_renewal_enabled` | 追加续期决定，不预占额度 |
| `reservation_operations.post_renewed` | 首次预占开关及 `medops_internal_renewed_reservation_write_enabled` | 基于当前有效续期的首次预占 |
| `reservation_operations.prepare_release` / `post_release` | `medops_internal_reservation_release_write_enabled` | 保存独立释放确认 / 追加释放决定及事件 |
| `reservation_operations.inspect` | 无额外财务写入开关 | 核验完整历史后返回当前位置 |

所有 `reservation_operations.*` 另需 `medops_reservation_operations_enabled is True`。缺失、数字1和字符串 `"true"` 都不能开启。开发、安装和迁移过程不自动开启任何生产开关；目前仅精确命名的一次性 CI 探针站点显式开启。

## 首次预占

1. 调用 `reservation.source`，传 `request`（原生 Material Request）、`line`（原生子行标识）、`organization_unit`，获取已授权且精确的 `source_snapshot` 与 `source_snapshot_digest`。原生来源变化后必须重新核对。
2. 调用 `reservation.prepare`，传完整快照、`allocation`、完整 `budget_key`、`currency`、整数 `precision`、精确字符串 `amount`、`budget_date`、`reason`。`budget_key` 包含 company、hospital、organization_unit、fiscal_year、budget_subject、funding_source、project、period_start、period_end。该方法重验来源、下达和完整占用历史后保存独立确认，返回供人工核对的 review、confirmation、proof、expires_at 和 intent_digest；effect 为 `confirmation_only`。
3. 展示来源、预算、币种、金额、日期和理由，由当前人类明确勾选确认。调用 `reservation.confirm`，传 `{confirmation, proof, acknowledge: true}`，取得 `decision`。成功仅表示独立决定已保存，尚未预占额度。
4. 调用 `reservation_operations.post`，传 `{confirmation, decision, proof, acknowledge: true}`。成功返回 `reservation`、`event`、`intent_digest`、审计标识和 `idempotency_replayed`；effect 为 `reservation_posted`。首次根与第一条事件在同一 HTTP 事务中落账。

第3步和第4步是独立事务，必须分别保存同一命令的幂等键。第4步失败不能把第3步的成功显示成预占成功。不得自动更换 allocation 或键来绕过状态冲突。

## 决定续期

只针对尚未产生首次根且原决定已经过期的原意图。新核对预览本身必须在前一决定到期后创建，提前创建的预览不能留到以后充当续期。保留原 `confirmation` 与 `decision`，使用完全相同的原意图重新调用 `reservation.prepare`，取得新的独立确认和会话凭证并重新展示人工核对。

人工明确确认后调用 `reservation_operations.renew`，传 `original_confirmation`、`original_decision`、`renewal_confirmation`、新 `proof` 和 `acknowledge: true`。成功返回 `renewal`、intent_digest、expires_at，effect 为 `decision_renewed`，仍没有预占额度。

随后调用 `reservation_operations.post_renewed`，传相同字段并增加 `renewal_decision: renewal`。成功才显示 `reservation_posted`。过期、来源/权限变化、已有根、陈旧续期链或旧原始决定都不能创建新财务事件。续期不得自动触发；改变原经济意图需要新的业务核对，不能伪装成续期。

## 查询与释放

`reservation_operations.inspect` 只接收 `{reservation}`。它独立读取原始确认、决定、全部续期、全部物理事件及释放确认/决定，经过同一来源和预算围栏后返回：

- `position`：预算键、币种/精度、当前序号及 reserved、committed、actual、released、open_reservation、open_commitment 等精确字符串；
- 受限 `source` 事实：采购单、子行、物料、单位、数量、金额、成本中心、费用科目；
- allocation、budget_date，effect 为 `read_only`。

根单的旧金额、旧事件数量和旧链尾只描述首笔事实，不能用于替代当前位置。查询不是财务授权，后续写入必须重新核验。

释放先调用 `reservation_operations.prepare_release`，传 `{reservation, amount, reason}`。返回 confirmation、proof、expires_at、intent_digest、reservation、amount、currency、precision、open_before 和规范化 reason；effect 为 `confirmation_only`。只允许正数，绑定当前前驱与未结余额，最大五分钟。

展示已核验来源、预占编号、当前未结、精确释放金额、币种和理由，由当前人类明确确认后调用 `reservation_operations.post_release`，传 `{confirmation, proof, acknowledge: true}`。成功返回 reservation、release_decision、event、intent_digest 和审计/重放标识，effect 为 `reservation_released`。独立决定和释放事件同一事务追加；根单不改写。状态已被其他合法释放推进时，旧预览不能用于新写入，必须重新核对。

## 失败、重试与凭证

普通失败由最外层 Frappe 请求回滚；领域服务无提交。释放决定之后失败也不能留下单边决定、事件或已完成回执。请求开始已有写入、回调、围栏、读图或嵌套事务控制时拒绝，不能悄悄回滚其他工作。

网络结果不明时，保留相同输入、会话、凭证和幂等键原样重试，或调查已有持久结果。原生数据库死锁/超时只允许最外层在完整事务回滚后重试。不得在同一已失败事务中继续后续步骤。相同键换输入拒绝；当前权限和完整历史在重放时仍须有效。

历史已完成回执可验证当时的合法决定而重放，不延长任何新写入的凭证有效期。以后发生合法释放后，原首笔和早先释放的回执仍须核验完整当前历史；这不是允许使用过期凭证创建新事件。

proof 只在页面私有内存保存，不能显示、复制进 URL、写浏览器存储、日志或通用对象序列化。预览响应提取后删除公开对象中的 proof。编辑输入清除旧确认，异步返回须比较页面代次；尚未核实的提交结果应使用原命令重试。前置隐私钩子覆盖四个新凭证方法及原有确认方法，屏蔽 form/JSON 与原生 SDK/错误日志诊断视图。

## 当前验证范围

固定候选本地617项纯测试与全部11步质量检查通过。首轮原生专项40671936652发现新测试夹具的 `super()` 复用错误及请求拒绝状态码断言错误，不能视为通过；业务路由和四方法认证前 SDK/日志隐私子集已执行通过。修正夹具和400断言后的专项40673023305针对6cc9414仍在运行；修正源码本地617项及全部11步质量再次通过，类型零错误、一项既有警告。

真实财务 HTTP 仅在严格限制的一次性 CI 新站点中验证，使用合成业务数据和实际原生会话、来源、预算及控制器；不等于真实人员操作、浏览器验收或医院容量。普通 CI 测试只验证边界、路由和隐私，不能留下合成已提交不可变财务事实。目标服务器仍在用户维护中，本轮未连接或部署。


## 页面候选与当前限制

标准路由 `hrp-budget-reservations` 从下达/冲销页面进入，限制既有管理角色。先选择有原生权限的采购明细，再从完整且有范围权限的历史下达账簿选择匹配预算；历史行不是可用余额，写入由服务器重新验证。独立核对窗口展示来源、预算键、币种、精确金额、日期、理由和有效期，默认未勾选。首次决定、续期决定、实际过账分别处理；只有实际过账成功才显示财务完成。释放先查询完整当前位置，再生成并确认独立释放预览。

页面私有 journal 保留原命令/凭证/幂等键，结果不明时不能用编辑后的输入再发起另一命令；输入变化后的恢复只核实原单一请求，不隐式执行下一阶段。后续权限拒绝不能证明先前未知请求没有成功。原生表单和所有输出使用文本，金额不转浮点；不使用 URL、浏览器持久存储或全局可枚举对象保存凭证。

13项 Node 测试执行实际 JS 页面及 helper，使用控件/DOM 协议替身。新增原生Page同步/既有角色/证据权限闭合检查待隔离验证。刷新后或会话变更后恢复已确认但未过账的目录尚未实现；不能通过新分摊或换键规避未核实结果。真实Desk渲染、浏览器验收、全部写入口和医院容量仍未验证，生产开关保持关闭。

40673023305的6项原生边界/HTTP/SDK隐私通过；已提交探针完成首笔、重复回执、释放和回滚后，在提前续期处因正确的未到期拒绝而失败。已修正测试为先断言409，再模拟时间超过原到期并新建确认。新专项40678619220针对996b70b仍在运行；原生会话、来源、预算和控制器是真实，续期时间为测试模拟，不等于真实人/医院验收。

页面命令journal通过原生 `medops_reservation_session.bundle.js` 内容哈希资源加载，部署须正常构建资产并刷新站点缓存；不依赖未版本化脚本URL。原生元数据用例还检查资产清单、实际构建文件及journal内容，资源隔离验证待完成。

最终事务专项40678619220通过：7项常规原生、13前端及1项已提交探针616.625秒，无跳过，两站点Error Log零。提前续期拒绝和模拟到期后的新确认/续期/预占通过；原生会话与来源/预算/控制器为真实，续期时钟模拟。资源专项40680330337通过7原生+13前端及原生hash清单/编译内容检查，Error Log零。完整当前源码CI、实际Desk浏览器及医院验收仍未完成。

## 当前授权下的恢复目录候选（ADR-0040）

`reservation_operations.recovery` 是 POST 只读查询；请求必需 source_snapshot、source_snapshot_digest、budget_key，可选 offset、limit、expected_digest、decision。每页最多20条、同一来源全部原决定最多100条；后续页必须带完整目录摘要。指定决定也完成同一来源/预算的核验，不能绕过范围、原生字段权限或证据完整性。接口要求当前管理角色、预占写范围、台账读范围及当前原生来源权限，不增加用户授予。

返回当前来源摘要、预算键、目录摘要/分页及经过核验的记录。Posted只提供已核验根引用；Decision Valid显示最新独立决定的有效期，不能提前续期；Expired提供恢复原意图所需的六项字段，必须在新会话中新生成确认并人工勾选；Source Changed不得续期。没有 proof/proof_hash/session_digest/私有快照JSON/一次性键，不读取旧回执密文，不写财务或幂等记录。

Page选定来源和预算后先查历史，恢复按钮再次读取指定决定，若已经并发过账则显示已有结果。恢复原allocation而非悄悄生成新分摊；已有历史时新增独立分摊必须明确选择并再次人工核对。页面内原未知命令仍只能原样重试；目录读取不会自动清除其不确定状态。导航优先显示明确路由，提供单独查看上次完成结果。

当前622项纯测试（含4项恢复解析）、19项前端执行和12个质量组件通过；v3末步格式失败以纯排版修复后通过，保留记录。原生临时证据/真实Cookie已提交恢复待隔离验证。真实Desk浏览器、全部写入口、医院规则及容量仍未验证，生产开关关闭；服务器维护期间不部署。

未知确认请求重试得到已保存决定后，也会清空早先的空目录缓存，必须重新核验历史后才能发起另一预览。上一df963f9源码完整CI40682448628的两套617+29常规原生、已提交探针、八站点零Error Log及Required已通过，不覆盖新增恢复及导航；结果按源码分别保存。

恢复接口专项40694061543（2af1a71）已通过4+4+3常规原生、18前端和1项已提交探针1162.151秒，两个隔离站点Error Log零；6个财务应用/HTTP辅助文件与产品4b70f5a一致。最终原生测试类型/断言修复和新增的第19项前端由当前产品全量CI40696687032继续验证，Quality/Security已通过，两套原生运行。另行CI-only真实Chromium/Desk只读显示/刷新/输入窗与安全逐请求计时40698778414运行，不新增产品依赖、不连接服务器；复杂探针总时长不是单笔延迟或医院容量证明。

## 新建Bench的原生资源前提

CI真实登录发现仅编译MedOps会遗漏Frappe网站/登录/Desk核心资源，导致$未定义和无样式。新建Bench须在三个锁定生态应用和MedOps（及所选Core）获取完毕后构建全部应用；原生边界核验frappe-web/desk JS和website/login/desk CSS均在清单内且实际非空，再检查自定义内容哈希bundle。真实登录/空Page重试尚待完成，已有后端绿色结果不能证明浏览器可用。本轮不修改上游或新增产品依赖，服务器维护期间不部署。

## 实际空页面验证与性能实验边界（2026-10-29）

真实空Desk专项40821426381在519c23c通过：原生密码登录、明确Page入口、无阻挡弹窗/JS/本地资源/RPC错误、无财务命令，财务全表快照一致、ErrorLog零并原生注销；截图人工视觉核对干净。先前40816808093虽然CI返回成功，截图仍有Page null弹窗，故拒绝作为干净UI证据；40818500994初始化收尾后严格门禁失败，40820539001路由追踪亦失败。根因是CI等待表达式以frappe.set_route函数结尾，Playwright会自动调用返回的函数并传null；改成布尔判断，已本地复现并对照锁定工具源码确认，不修改产品/Frappe。完整17781ac/40816048987 Quality/Security已通过，两套原生仍活跃。合法根显示/刷新/释放输入新专项40822250244针对d74a028运行；只读选择由当前Cookie完整检查，不以原根旧投影当当前位置，不执行浏览器财务写入。Core浏览器、恢复控件与医院验收仍未证明。

性能隔离实验40820688375/a1521cf在29个模型上以每次新读原始配置和原生Meta处理得到完整字典零差异；每批SQL342降至174，三次中位构造耗时0.255582降至0.153388秒（约39.98%），自身事务的DocField权限级别/DocPerm读取权限变化及回滚均可见，财务快照未改、ErrorLog零。普通Document克隆及官方get_docs后克隆均有29个字典差异，包含丢失/新增字段以及show_preview_popup由整数变None，未放宽比较来使其通过。前两次40817290822缺DocType身份、40818511295完整等价失败均保留。原生原始构造方案仍是CI-only实验，未接入产品，不是财务权限/撤权矩阵、实际接口延迟或医院容量证明；下一步须审查固定源码耦合并做真实权限与完整财务/普通/Core验证。维护期间未连接服务器，无迁移、权限或财务逻辑改变。

2026-10-29: Product524557a full40832608462 Quality/Security passed; exact plain/Core native remains running. Exact-runtime Core40832870602/e5a0270 passed19 regular native,19 frontend and1 committed probe483.601s, then actual Chromium password-login/current-root display/reload/release-input/logout;3 inspect200,zero JS/local errors/financial commands,unchanged full financial snapshots,ErrorLog0,Core source/sealed inventory verified,screenshot visually reviewed.35 native cookie requests timed without altering assertions; successful recovery median8.138741s(n7),inspect8.409846s(n3),initial post6.882094s(n3),release preview12.876039s(n3),release post13.776089s(n3). Shared disposable WSGI CI observations exclude network transit and do not establish hospital SLO/capacity or controlled before/after percentage. Plain profile still running. Fresh posted-recovery Page controls40834757012/1e114d1 now running both profiles; no pass claimed. No server access/deploy/grants/schema change.
