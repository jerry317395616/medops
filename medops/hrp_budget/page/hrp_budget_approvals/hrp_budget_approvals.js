frappe.pages["hrp-budget-approvals"].on_page_load = (wrapper) => {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("预算审批"), single_column: true });
	const filters = $("<div class='row mt-3'>").appendTo(page.body);
	const body = $("<div class='mt-3'>").appendTo(page.body);
	const controls = {};
	let offset = 0,
		ready = false;
	const status = (value) =>
		({ Pending: __("待审批"), Approved: __("已批准"), Rejected: __("已驳回"), Withdrawn: __("已撤回") })[
			value
		] || value;
	const actionLabel = (value) =>
		({ Approve: __("批准"), Reject: __("驳回"), Withdraw: __("撤回") })[value] || value;
	const has = (...roles) => roles.some((role) => frappe.user.has_role(role));
	let generation = 0;
	const call = async (method, args, key) =>
		(
			await frappe.call({
				method: "medops.api.v1.finance.approval." + method,
				args,
				type: key ? "POST" : "GET",
				headers: key ? { "Idempotency-Key": key } : undefined,
				freeze: true,
			})
		).message;
	const commandKey = () => `budget-review-${frappe.utils.get_random(24)}`;
	function table(parent, headings) {
		const table = $("<table class='table table-bordered table-sm'>").appendTo(
			$("<div class='table-responsive'>").appendTo(parent),
		);
		const header = $("<tr>").appendTo($("<thead>").appendTo(table));
		for (const title of headings) $("<th>").text(__(title)).appendTo(header);
		return $("<tbody>").appendTo(table);
	}
	function fact(parent, label, value) {
		$("<p>")
			.text(`${__(label)}：${value ?? ""}`)
			.appendTo(parent);
	}
	function decision(doc, action) {
		const key = commandKey();
		const dialog = new frappe.ui.Dialog({
			title: actionLabel(action),
			fields: [{ fieldname: "reason", label: __("核对结论与理由"), fieldtype: "Small Text", reqd: 1 }],
			primary_action_label: __("生成确认预览"),
			primary_action: async (values) => {
				if (!values.reason || values.reason.length > 1000)
					return frappe.msgprint(__("请填写不超过 1000 字的理由。"));
				const preview = await call(
					"prepare",
					{ payload: { case: doc.name, expected_revision: doc.revision, action, reason: values.reason } },
					key,
				);
				dialog.hide();
				confirmPreview(preview);
			},
		});
		dialog.show();
	}
	function confirmPreview(preview) {
		const key = commandKey();
		let proof = preview.proof;
		const dialog = new frappe.ui.Dialog({
			title: __("人工确认预算决定"),
			size: "large",
			fields: [
				{ fieldname: "facts", fieldtype: "HTML" },
				{
					fieldname: "acknowledge",
					label: __("我已核对申报事实、精确金额和本次决定，确认提交"),
					fieldtype: "Check",
					default: 0,
				},
			],
			primary_action_label: __("确认提交决定"),
			primary_action: async (values) => {
				if (!values.acknowledge || !proof) return frappe.msgprint(__("请先核对事实并勾选人工确认。"));
				await call(
					"confirm",
					{ payload: { confirmation: preview.confirmation, proof, acknowledge: true } },
					key,
				);
				dialog.hide();
				await showCase(preview.case);
			},
			onhide: () => {
				proof = null;
			},
		});
		const content = dialog.fields_dict.facts.$wrapper;
		fact(content, "决定", actionLabel(preview.action));
		fact(content, "申报金额", `${preview.currency} ${preview.total_amount}`);
		fact(content, "核对理由", preview.reason);
		fact(content, "确认有效期至", preview.expires_at);
		const evidence = $("<details>").appendTo(content);
		$("<summary>").text(__("申报凭证")).appendTo(evidence);
		fact(evidence, "审批单", preview.case);
		fact(evidence, "申报事实摘要", preview.source_digest);
		dialog.show();
	}
	async function showCase(name) {
		const current = ++generation;
		filters.hide();
		body.empty();
		const doc = await call("get_record", { name });
		if (current !== generation) return;
		$("<h3>").text(doc.subject).appendTo(body);
		fact(body, "状态", `${status(doc.status)} · ${__("第")} ${doc.round_number} ${__("轮")}`);
		fact(body, "金额", `${doc.currency} ${doc.total_amount}`);
		fact(body, "发起人", doc.requested_by);
		$("<p class='text-muted'>")
			.text(__("审批保留申报事实与独立复核记录；可用预算需完成后续预算下达。"))
			.appendTo(body);
		const source = $("<details>").appendTo(body);
		$("<summary>").text(__("申报来源与审批依据")).appendTo(source);
		for (const [label, value] of [
			["申报单", doc.submission],
			["医院", doc.hospital],
			["组织快照", doc.organization_version],
			["科室", doc.organization_unit],
			["预算年度", doc.fiscal_year],
			["申报事实摘要", doc.source_digest],
			["审批矩阵", doc.policy_matrix],
			["矩阵版本", doc.policy_revision],
		])
			fact(source, label, value);
		const rows = table(body, ["步骤", "会签方式", "审批人", "进度"]);
		for (const step of doc.steps) {
			const row = $("<tr>").appendTo(rows);
			const progress =
				doc.status === "Pending"
					? step.sequence_no === doc.current_step
						? __("当前步骤")
						: step.sequence_no < doc.current_step
							? __("已通过")
							: __("待前序通过")
					: status(doc.status);
			for (const text of [
				step.step_name,
				step.approval_mode === "All" ? __("全部通过") : __("任一通过"),
				step.approvers.join("、"),
				progress,
			])
				$("<td>").text(text).appendTo(row);
		}
		if (doc.status === "Pending") {
			const currentStep = doc.steps.find((row) => row.sequence_no === doc.current_step);
			const forbidden = [doc.applicant, doc.source_applicant, doc.requested_by].includes(frappe.session.user);
			const actions = [];
			if (
				!forbidden &&
				currentStep?.approvers.includes(frappe.session.user) &&
				!doc.current_voters.includes(frappe.session.user)
			)
				actions.push("Approve", "Reject");
			if (
				doc.requested_by === frappe.session.user ||
				has("System Manager", "HRP System Manager", "HRP Budget Manager")
			)
				actions.push("Withdraw");
			for (const action of actions)
				$("<button class='btn btn-default mr-2 mb-3'>")
					.text(actionLabel(action))
					.on("click", () => decision(doc, action))
					.appendTo(body);
		}
		$("<h4>").text(__("审批记录")).appendTo(body);
		if (!doc.reviews.length) $("<p>").text(__("尚无已确认的决定。")).appendTo(body);
		else {
			const history = table(body, ["步骤", "审批人", "决定", "理由", "时间"]);
			for (const event of doc.reviews) {
				const row = $("<tr>").appendTo(history);
				for (const text of [
					event.sequence_no,
					event.actor,
					actionLabel(event.action),
					event.reason,
					event.recorded_at,
				])
					$("<td>").text(text).appendTo(row);
			}
		}
	}
	async function refresh() {
		const name = frappe.get_route()[1];
		if (name) return showCase(name);
		const current = ++generation;
		filters.show();
		body.empty();
		const selected = Object.fromEntries(
			Object.entries(controls)
				.map(([key, control]) => [key, control.get_value()])
				.filter(([, value]) => value),
		);
		if (selected.status)
			selected.status = Object.keys({ Pending: 1, Approved: 1, Rejected: 1, Withdrawn: 1 }).find(
				(value) => status(value) === selected.status,
			);
		const result = await call("list_records", { filters: selected, offset, limit: 50 });
		if (current !== generation) return;
		$("<p>").text(__("仅列出当前有权读取全部来源的审批单。请按法人、医院和状态筛选。")).appendTo(body);
		if (!result.items.length) {
			$("<p>").text(__("暂无可见审批单。")).appendTo(body);
			if (offset > 0)
				$("<button class='btn btn-default'>")
					.text(__("返回第一页"))
					.on("click", () => {
						offset = 0;
						refresh();
					})
					.appendTo(body);
			return;
		}
		const rows = table(body, ["申报", "金额", "发起人", "操作"]);
		for (const item of result.items) {
			const row = $("<tr>").appendTo(rows);
			for (const text of [item.subject, `${item.currency} ${item.total_amount}`, item.requested_by])
				$("<td>").text(text).appendTo(row);
			$("<button class='btn btn-sm btn-default'>")
				.text(__("核对与审批"))
				.on("click", () => frappe.set_route("hrp-budget-approvals", item.name))
				.appendTo($("<td>").appendTo(row));
		}
		if (offset > 0)
			$("<button class='btn btn-default mr-2'>")
				.text(__("上一页"))
				.on("click", () => {
					offset = Math.max(0, offset - 50);
					refresh();
				})
				.appendTo(body);
		if (result.next_offset !== null)
			$("<button class='btn btn-default'>")
				.text(__("下一页"))
				.on("click", () => {
					offset = result.next_offset;
					refresh();
				})
				.appendTo(body);
	}
	for (const df of [
		{ fieldname: "company", label: __("法人"), fieldtype: "Link", options: "Company" },
		{
			fieldname: "hospital",
			label: __("医院"),
			fieldtype: "Link",
			options: "HRP Hospital",
			get_query: () => ({ filters: { company: controls.company.get_value() } }),
		},
		{
			fieldname: "status",
			label: __("状态"),
			fieldtype: "Select",
			options: ["", ...["Pending", "Approved", "Rejected", "Withdrawn"].map(status)],
			default: status("Pending"),
		},
	]) {
		controls[df.fieldname] = frappe.ui.form.make_control({
			parent: $("<div class='col-md-4'>").appendTo(filters),
			df: {
				...df,
				onchange: () => {
					if (!ready) return;
					offset = 0;
					if (df.fieldname === "company" && controls.hospital.get_value())
						return controls.hospital.set_value("");
					refresh();
				},
			},
			render_input: true,
		});
	}
	ready = true;
	page.add_inner_button(__("审批列表"), () =>
		frappe.get_route()[1] ? frappe.set_route("hrp-budget-approvals") : refresh(),
	);
	page.add_inner_button(__("刷新"), refresh);
	wrapper.budgetApprovalRefresh = refresh;
};
frappe.pages["hrp-budget-approvals"].on_page_show = (wrapper) => wrapper.budgetApprovalRefresh?.();
