frappe.pages["hrp-budget-operations"].on_page_load = (wrapper) => {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("预算下达与冲销"), single_column: true });
	const filters = $("<div class='row mt-3'></div>").appendTo(page.main);
	const tools = $("<div class='d-flex flex-wrap mb-3'></div>").appendTo(page.main);
	const body = $("<div class='pb-3'></div>").appendTo(page.main);
	const controls = {};
	let generation = 0,
		kind = "records",
		directory = null,
		activeDialog = null,
		writeBusy = false,
		pendingReads = 0,
		syncing = false;
	const canManage = () =>
		["System Manager", "HRP System Manager", "HRP Budget Manager"].some((role) => frappe.user.has_role(role));
	const statusLabel = (value) =>
		({ Prepared: __("待人工确认"), Authorized: __("已授权，等待处理"), Posted: __("已过账") })[value] ||
		value;
	const actionLabel = (value) => (value === "Reverse" ? __("完整冲销") : __("预算下达"));
	const fact = (parent, label, value) =>
		$("<p></p>")
			.text(`${__(label)}：${value ?? ""}`)
			.appendTo(parent);
	function button(parent, label, action, primary = false) {
		return $("<button type='button' class='btn mr-2 mb-2'></button>")
			.addClass(primary ? "btn-primary" : "btn-default")
			.text(__(label))
			.on("click", action)
			.appendTo(parent);
	}
	function invalidate() {
		generation += 1;
		directory = null;
		activeDialog?.hide();
		activeDialog = null;
		body.empty();
	}
	function selectFilters() {
		return Object.fromEntries(
			Object.entries(controls)
				.map(([name, control]) => [name, control.get_value()])
				.filter(([, value]) => value),
		);
	}
	function updateTools() {
		tools.find("button").prop("disabled", writeBusy || pendingReads > 0);
	}
	async function read(method, args) {
		pendingReads += 1;
		updateTools();
		try {
			return (await frappe.call({ method: `medops.api.v1.finance.release.${method}`, type: "GET", args }))
				.message;
		} finally {
			pendingReads -= 1;
			updateTools();
		}
	}
	// Keep a stable key for identical input after an uncertain network result.
	// A changed input starts a new command, still guarded by server revisions.
	function commandSender(method) {
		let signature = null,
			commandKey = null;
		return async (payload) => {
			if (writeBusy) throw new Error("An operation is already pending");
			const next = JSON.stringify(
				method === "confirm"
					? { confirmation: payload.confirmation, acknowledge: payload.acknowledge }
					: payload,
			);
			if (next !== signature) {
				signature = next;
				commandKey = `budget-operation-${frappe.utils.get_random(32)}`;
			}
			writeBusy = true;
			updateTools();
			try {
				return (
					await frappe.call({
						method: `medops.api.v1.finance.release.${method}`,
						type: "POST",
						args: { payload },
						headers: { "Idempotency-Key": commandKey },
					})
				).message;
			} finally {
				writeBusy = false;
				updateTools();
			}
		};
	}
	function table(parent, headings) {
		const grid = $("<table class='table table-bordered table-sm'></table>").appendTo(
			$("<div class='table-responsive'></div>").appendTo(parent),
		);
		const row = $("<tr></tr>").appendTo($("<thead></thead>").appendTo(grid));
		for (const heading of headings) $("<th class='text-nowrap'></th>").text(__(heading)).appendTo(row);
		return $("<tbody></tbody>").appendTo(grid);
	}
	function retryHint(parent) {
		parent.find(".operation-retry").remove();
		$("<p role='status' class='text-muted operation-retry'></p>")
			.text(__("提交结果尚未确认。可用相同输入重试，或关闭后刷新凭证检查状态。"))
			.appendTo(parent);
	}
	function intentDialog(method, payloadFor, title, initial = {}) {
		if (!canManage() || writeBusy) return;
		const id = generation,
			send = commandSender(method);
		const dialog = new frappe.ui.Dialog({
			title: __(title),
			fields: [
				{
					fieldname: "posting_date",
					label: __("业务日期"),
					fieldtype: "Date",
					reqd: 1,
					default: initial.posting_date || frappe.datetime.get_today(),
				},
				{
					fieldname: "reason",
					label: __("核对理由"),
					fieldtype: "Small Text",
					reqd: 1,
					default: initial.reason || "",
				},
			],
			primary_action_label: __("保存待确认凭证"),
			primary_action: async (values) => {
				if (id !== generation || writeBusy) return;
				if (!values.reason?.trim() || values.reason.length > 1000)
					return frappe.msgprint(__("请填写不超过1000字的核对理由。"));
				dialog.get_primary_btn().prop("disabled", true);
				try {
					const result = await send(payloadFor({ posting_date: values.posting_date, reason: values.reason }));
					if (id !== generation) return;
					dialog.hide();
					if (frappe.get_route()[1] === result.name) await showRecord(result.name);
					else frappe.set_route("hrp-budget-operations", result.name);
				} catch {
					if (id === generation) retryHint(dialog.$body);
				} finally {
					dialog.get_primary_btn().prop("disabled", false);
				}
			},
			onhide: () => {
				if (activeDialog === dialog) activeDialog = null;
			},
		});
		fact(dialog.$body, "保存后仍需独立人工确认", __("请核对来源和日期，确认后由后台整批处理。"));
		activeDialog?.hide();
		activeDialog = dialog;
		dialog.show();
	}
	async function prepareConfirmation(doc) {
		if (!canManage() || writeBusy) return;
		const id = generation,
			send = commandSender("prepare");
		try {
			const preview = await send({ release: doc.name, expected_revision: doc.revision });
			if (id !== generation) {
				delete preview.proof;
				return;
			}
			confirmPreview(preview);
		} catch {
			if (id === generation) frappe.msgprint(__("确认预览未能生成，请刷新凭证并重新核对。"));
		}
	}
	function confirmPreview(preview) {
		const id = generation,
			send = commandSender("confirm");
		let proof = preview.proof;
		delete preview.proof;
		const dialog = new frappe.ui.Dialog({
			title: __("人工确认预算操作"),
			size: "large",
			fields: [
				{ fieldname: "facts", fieldtype: "HTML" },
				{
					fieldname: "acknowledge",
					label: __("我已核对完整来源、精确金额、业务日期和本次操作，确认提交"),
					fieldtype: "Check",
					default: 0,
				},
			],
			primary_action_label: __("确认并授权处理"),
			primary_action: async (values) => {
				if (id !== generation || writeBusy || !proof) return;
				if (!values.acknowledge) return frappe.msgprint(__("请先核对事实并勾选人工确认。"));
				dialog.get_primary_btn().prop("disabled", true);
				try {
					await send({ confirmation: preview.confirmation, proof, acknowledge: true });
					if (id !== generation) return;
					dialog.hide();
					await showRecord(preview.release);
				} catch {
					if (id === generation) retryHint(dialog.$body);
				} finally {
					dialog.get_primary_btn().prop("disabled", false);
				}
			},
			onhide: () => {
				proof = null;
				if (activeDialog === dialog) activeDialog = null;
			},
		});
		const facts = dialog.fields_dict.facts.$wrapper;
		for (const [label, value] of [
			["操作", actionLabel(preview.action)],
			["精确净额", `${preview.currency} ${preview.total_amount}`],
			["原始明细条数", preview.line_count],
			["业务日期", preview.posting_date],
			["核对理由", preview.reason],
			["执行人", frappe.session.user],
			["确认有效期至", preview.expires_at],
			["凭证编号", preview.release],
		])
			fact(facts, label, value);
		$("<p class='text-muted'></p>")
			.text(__("确认将授权本次完整下达或冲销；已授权的未执行凭证再次确认会替换原授权。"))
			.appendTo(facts);
		activeDialog?.hide();
		activeDialog = dialog;
		dialog.show();
	}
	async function showRecord(name) {
		invalidate();
		const id = generation;
		filters.hide();
		try {
			const doc = await read("get_record", { name });
			if (id !== generation) return;
			syncing = true;
			try {
				for (const [field, control] of Object.entries(controls)) await control.set_value(doc[field] || "");
			} finally {
				syncing = false;
			}
			if (id !== generation) return;
			$("<h3></h3>")
				.text(`${actionLabel(doc.operation)} · ${statusLabel(doc.status)}`)
				.appendTo(body);
			for (const [label, value] of [
				["精确净额", `${doc.currency} ${doc.total_amount}`],
				["原始明细条数", doc.line_count],
				["预算年度", doc.fiscal_year],
				["业务日期", doc.posting_date],
				["核对理由", doc.reason],
				["准备人", doc.prepared_by],
				["授权人", doc.authorized_by],
				["过账时间", doc.posted_at],
			])
				fact(body, label, value);
			const source = $("<details class='mb-3'></details>").appendTo(body);
			$("<summary></summary>").text(__("来源与凭证")).appendTo(source);
			for (const [label, value] of [
				["凭证编号", doc.name],
				["审批单", doc.source_approval],
				["凭证所属组织", doc.organization_unit],
				["凭证修订", doc.revision],
			])
				fact(source, label, value);
			button(body, "核对完整申报来源", () =>
				frappe.set_route("hrp-budget-consolidations", doc.source_submission),
			);
			button(body, "刷新状态", () => showRecord(doc.name));
			if (doc.operation === "Reverse")
				button(body, "查看原下达", () => frappe.set_route("hrp-budget-operations", doc.reversal_of));
			if (doc.status === "Authorized")
				$("<p role='status'></p>")
					.text(
						__("本次操作已获授权，后台将按完整批次处理。请刷新查看过账状态；长时间未完成时联系管理员检查。"),
					)
					.appendTo(body);
			if (doc.status === "Prepared" && canManage())
				button(body, "修订日期和理由", () =>
					intentDialog(
						"revise",
						(values) => ({ release: doc.name, expected_revision: doc.revision, ...values }),
						"修订待确认凭证",
						doc,
					),
				);
			if (["Prepared", "Authorized"].includes(doc.status) && canManage())
				button(
					body,
					doc.status === "Authorized" ? "重新核对并确认" : "核对并生成确认预览",
					() => prepareConfirmation(doc),
					true,
				);
			if (doc.status === "Posted" && doc.operation === "Release") {
				const related = await read("list_records", {
					filters: {
						company: doc.company,
						hospital: doc.hospital,
						fiscal_year: doc.fiscal_year,
						reversal_of: doc.name,
					},
					limit: 1,
				});
				if (id !== generation) return;
				if (related.items.length)
					button(body, "查看完整冲销凭证", () =>
						frappe.set_route("hrp-budget-operations", related.items[0].name),
					);
				else if (canManage())
					button(body, "准备完整冲销", () =>
						intentDialog(
							"create_reversal",
							(values) => ({ release: doc.name, expected_revision: doc.revision, ...values }),
							"准备完整冲销",
						),
					);
			}
		} catch {
			if (id === generation) {
				body.empty();
				fact(body, "读取未完成", __("请刷新并检查当前来源及凭证权限。"));
			}
		}
	}
	async function list(offset = 0) {
		if (writeBusy || pendingReads) return;
		const selected = selectFilters();
		if (!selected.company || !selected.hospital || !selected.fiscal_year)
			return frappe.msgprint(__("请先选择法人、医院和预算年度。"));
		const digest = offset ? directory?.list_digest : null;
		invalidate();
		const id = generation;
		filters.show();
		const loading = $("<p role='status' class='text-muted'></p>")
			.text(__("正在核验完整来源及当前权限，请稍候……"))
			.appendTo(body);
		try {
			const result = await read(kind === "eligible" ? "eligible_sources" : "list_records", {
				filters: selected,
				offset,
				limit: 50,
				expected_digest: digest,
			});
			if (id !== generation) return;
			loading.remove();
			directory = result;
			$("<h3></h3>")
				.text(kind === "eligible" ? __("待下达的已批准汇总") : __("下达与冲销凭证"))
				.appendTo(body);
			$("<p class='text-muted'></p>")
				.text(__("仅展示当前有权核对完整来源的记录。准备、确认和过账时仍会核验业务日期及当前权限。"))
				.appendTo(body);
			if (!result.items.length) fact(body, "当前范围", __("暂无可见记录。"));
			const rows = table(body, ["事项", "精确净额", "状态", "操作"]);
			for (const item of result.items) {
				const row = $("<tr></tr>").appendTo(rows);
				for (const text of [
					item.subject,
					`${item.currency} ${item.total_amount}`,
					kind === "eligible" ? __("已批准汇总") : statusLabel(item.status),
				])
					$("<td></td>").text(text).appendTo(row);
				const actions = $("<td></td>").appendTo(row);
				if (kind === "records")
					button(actions, "核对凭证", () => frappe.set_route("hrp-budget-operations", item.name));
				else {
					button(actions, "核对来源", () => frappe.set_route("hrp-budget-consolidations", item.submission));
					if (canManage())
						button(actions, "准备下达", () =>
							intentDialog(
								"create",
								(values) => ({
									approval_case: item.name,
									expected_approval_revision: item.revision,
									...values,
								}),
								"准备预算下达",
							),
						);
				}
			}
			if (offset) button(body, "上一页", () => list(Math.max(0, offset - 50)));
			if (result.next_offset !== null) button(body, "下一页", () => list(result.next_offset));
		} catch {
			if (id === generation) {
				loading.remove();
				fact(body, "查询未完成", __("请缩小范围或检查权限；目录变化时重新查询第一页。"));
			}
		}
	}
	for (const [fieldname, label, options] of [
		["company", "法人", "Company"],
		["hospital", "医院", "HRP Hospital"],
		["fiscal_year", "预算年度", "Fiscal Year"],
		["organization_unit", "凭证所属组织", "HRP Organization Unit"],
		["currency", "币种", "Currency"],
	]) {
		controls[fieldname] = frappe.ui.form.make_control({
			parent: $("<div class='col-sm-6 col-lg-4'></div>").appendTo(filters),
			df: {
				fieldname,
				label: __(label),
				fieldtype: "Link",
				options,
				onchange: () => {
					if (!syncing) {
						invalidate();
						if (pendingReads) fact(body, "筛选已修改", __("当前读取结束后请重新查询。"));
					}
				},
			},
			render_input: true,
		});
	}
	function navigateList(selectedKind) {
		if (writeBusy || pendingReads) return;
		kind = selectedKind;
		if (frappe.get_route()[1]) frappe.set_route("hrp-budget-operations");
		else list();
	}
	button(tools, "凭证目录", () => navigateList("records"));
	button(tools, "待下达汇总", () => navigateList("eligible"));
	button(tools, "预算账簿", () => frappe.set_route("hrp-budget-statements"));
	wrapper.budgetOperationsRefresh = async () => {
		invalidate();
		const name = frappe.get_route()[1];
		if (name) return showRecord(name);
		filters.show();
		const selected = selectFilters();
		if (selected.company && selected.hospital && selected.fiscal_year) return list();
		fact(body, "操作提示", __("选择法人、医院和年度后查询。下达与冲销分别需要核对来源并明确人工确认。"));
	};
};
frappe.pages["hrp-budget-operations"].on_page_show = (wrapper) => {
	frappe.breadcrumbs.add({
		type: "Custom",
		label: __("预算下达与冲销"),
		route: "/desk/hrp-budget-operations",
	});
	return wrapper.budgetOperationsRefresh?.();
};
