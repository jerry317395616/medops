frappe.pages["hrp-budget-reservations"].on_page_load = async (wrapper) => {
	await frappe.require("/assets/medops/js/reservation_session.js");
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("采购预算预占"), single_column: true });
	const tools = $("<div class='d-flex flex-wrap my-3'></div>").appendTo(page.main);
	const status = $("<p role='status' class='text-muted'></p>").appendTo(page.main);
	const form = $("<div class='row'></div>").appendTo(page.main);
	const selection = $("<div class='mb-3'></div>").appendTo(page.main);
	const picker = $("<div class='mb-3'></div>").appendTo(page.main);
	const results = $("<div class='mb-3'></div>").appendTo(page.main);
	const controls = {};
	let source = null,
		budget = null,
		book = null,
		activeDialog = null,
		reads = 0,
		version = 0,
		syncing = false;
	let allocation = null;
	let lastOriginal = null,
		lastPosted = null;
	const journal = new frappe.medops.ReservationSession(
		async (method, payload, key) =>
			(
				await frappe.call({
					method: `medops.api.v1.finance.${method}`,
					type: "POST",
					args: { payload },
					headers: { "Idempotency-Key": key },
				})
			).message,
		() => `reservation-page-${frappe.utils.get_random(32)}`,
	);
	const fact = (parent, label, value) =>
		$("<p class='text-break'></p>")
			.text(`${__(label)}：${value ?? ""}`)
			.appendTo(parent);
	function button(parent, label, action, primary = false) {
		return $("<button type='button' class='btn mr-2 mb-2'></button>")
			.addClass(primary ? "btn-primary" : "btn-default")
			.text(__(label))
			.on("click", action)
			.appendTo(parent);
	}
	function blocked() {
		return journal.status.busy || journal.status.uncertain || reads > 0;
	}
	function update() {
		tools.find("button").prop("disabled", blocked() || !!activeDialog);
		resume.prop("disabled", journal.status.busy || !activeDialog);
		form.find("input,textarea,select,button").prop("disabled", blocked() || !!activeDialog);
		picker.find("button").prop("disabled", blocked() || !!activeDialog);
	}
	async function read(method, args, type = "GET") {
		reads += 1;
		update();
		try {
			return (await frappe.call({ method, args, type })).message;
		} finally {
			reads -= 1;
			update();
		}
	}
	async function send(method, payload) {
		const pending = journal.send(method, payload);
		update();
		try {
			return await pending;
		} finally {
			update();
		}
	}
	function changed(name) {
		if (syncing) return;
		version += 1;
		journal.invalidate();
		activeDialog?.hide();
		picker.empty();
		results.empty();
		book = null;
		if (["request", "organization_unit"].includes(name)) {
			source = null;
			budget = null;
			allocation = null;
		} else if (["fiscal_year", "currency"].includes(name)) {
			budget = null;
			allocation = null;
		}
		renderSelection();
	}
	function renderSelection() {
		selection.empty();
		if (source) {
			fact(
				selection,
				"采购来源",
				`${source.facts.request} · ${source.facts.item_code} · ${source.facts.line}`,
			);
			fact(selection, "原单精确金额", `${source.facts.currency} ${source.facts.amount}`);
		}
		if (budget)
			fact(
				selection,
				"选定预算",
				`${budget.subject} · ${budget.fund} · ${budget.key.period_start} — ${budget.key.period_end}`,
			);
	}
	for (const [fieldname, label, fieldtype, options] of [
		["request", "采购申请", "Link", "Material Request"],
		["organization_unit", "原始预算单位", "Link", "HRP Organization Unit"],
		["fiscal_year", "预算年度", "Link", "Fiscal Year"],
		["currency", "币种", "Link", "Currency"],
		["budget_date", "预算业务日期", "Date"],
		["amount", "精确预占金额", "Data"],
		["reason", "核对理由", "Small Text"],
		["reservation", "已有预占编号", "Data"],
	]) {
		controls[fieldname] = frappe.ui.form.make_control({
			parent: $("<div class='col-md-6'></div>").appendTo(form),
			df: {
				fieldname,
				label: __(label),
				fieldtype,
				options,
				onchange: () => changed(fieldname),
				...(fieldname === "request"
					? { get_query: () => ({ filters: { docstatus: 1, material_request_type: "Purchase" } }) }
					: {}),
			},
			render_input: true,
		});
		controls[fieldname].$input.on("input", () => changed(fieldname));
	}
	async function chooseSource(request, line, id) {
		try {
			const item = await read(
				"medops.api.v1.finance.reservation.source",
				{
					payload: { request, line, organization_unit: controls.organization_unit.get_value() },
				},
				"POST",
			);
			if (id !== version) return;
			const facts = JSON.parse(item.source_snapshot);
			if (facts.request !== request || facts.line !== line || facts.kind !== "material_request_source")
				throw new Error("Invalid source");
			source = { snapshot: item.source_snapshot, digest: item.source_snapshot_digest, facts };
			budget = null;
			allocation = null;
			book = null;
			picker.empty();
			syncing = true;
			try {
				await controls.currency.set_value(facts.currency);
				await controls.budget_date.set_value(facts.transaction_date);
			} finally {
				syncing = false;
			}
			renderSelection();
			status.text(__("来源已核对，请选择下达预算并填写金额和理由。"));
		} catch {
			if (id === version) status.text(__("来源无法核对，请检查采购单、原始预算单位和权限。"));
		}
	}
	async function lines() {
		if (blocked() || activeDialog) return;
		const request = controls.request.get_value();
		if (!request || !controls.organization_unit.get_value())
			return frappe.msgprint(__("请先选择采购申请和原始预算单位。"));
		version += 1;
		journal.invalidate();
		const id = version;
		source = null;
		budget = null;
		allocation = null;
		renderSelection();
		picker.empty();
		try {
			// Standard Frappe get applies parent read and field-level permissions.
			const doc = await read("frappe.client.get", { doctype: "Material Request", name: request });
			if (id !== version) return;
			if (doc.docstatus !== 1 || doc.material_request_type !== "Purchase" || !Array.isArray(doc.items))
				throw new Error("Invalid purchase source");
			function render(offset) {
				picker.empty();
				fact(
					picker,
					"采购明细",
					`${offset + 1} — ${Math.min(offset + 50, doc.items.length)} / ${doc.items.length}`,
				);
				for (const row of doc.items.slice(offset, offset + 50))
					button(picker, `${row.idx} · ${row.item_code || ""}`, () => {
						if (id === version && !blocked()) chooseSource(request, row.name, id);
					});
				if (offset) button(picker, "上一页", () => render(Math.max(0, offset - 50)));
				if (offset + 50 < doc.items.length) button(picker, "下一页", () => render(offset + 50));
			}
			render(0);
		} catch {
			if (id === version) status.text(__("采购明细无法读取，请检查原生采购单读取权限。"));
		}
	}
	async function budgets(offset = 0) {
		if (blocked() || activeDialog) return;
		if (!source || !controls.fiscal_year.get_value())
			return frappe.msgprint(__("请先选择采购明细和预算年度。"));
		const id = version;
		const query = {
			organization_unit: controls.organization_unit.get_value(),
			fiscal_year: controls.fiscal_year.get_value(),
			currency: controls.currency.get_value(),
		};
		try {
			const next = await read("medops.api.v1.finance.ledger.statement", {
				payload: JSON.stringify(query),
				offset,
				limit: 50,
				expected_digest: offset ? book?.statement_digest : null,
			});
			if (id !== version) return;
			book = next;
			picker.empty();
			fact(picker, "历史下达分录", __("选择与本采购明细相符的预算；当前能否预占由服务器再次核验。"));
			let matches = 0;
			next.rows.forEach((row, index) => {
				if (
					row.is_reversal ||
					!/^\d+(?:\.\d+)?$/.test(row.amount) ||
					!/[1-9]/.test(row.amount) ||
					row.organization_unit !== source.facts.organization_unit ||
					row.currency !== source.facts.currency ||
					row.account !== source.facts.expense_account ||
					row.cost_center !== source.facts.cost_center ||
					(row.project || null) !== (source.facts.project || null)
				)
					return;
				matches += 1;
				const labels = next.display_labels?.[index] || {};
				const subject = labels.budget_subject || row.budget_subject,
					fund = labels.funding_source || row.funding_source;
				button(picker, `${subject} · ${fund} · ${row.period_start} — ${row.period_end}`, async () => {
					if (id !== version || blocked()) return;
					version += 1;
					journal.invalidate();
					budget = {
						key: Object.fromEntries(
							[
								"company",
								"hospital",
								"organization_unit",
								"fiscal_year",
								"budget_subject",
								"funding_source",
								"project",
								"period_start",
								"period_end",
							].map((key) => [key, row[key]]),
						),
						precision: row.currency_precision,
						currency: row.currency,
						subject,
						fund,
					};
					allocation = `purchase-${frappe.utils.get_random(24)}`;
					if (!controls.amount.get_value()) {
						const [whole, fraction = ""] = source.facts.amount.split(".");
						if (!/[1-9]/.test(fraction.slice(budget.precision))) {
							syncing = true;
							try {
								await controls.amount.set_value(
									budget.precision
										? `${whole}.${fraction.slice(0, budget.precision).padEnd(budget.precision, "0")}`
										: whole,
								);
							} finally {
								syncing = false;
							}
						}
					}
					picker.empty();
					renderSelection();
				});
			});
			if (!matches)
				fact(picker, "本页没有匹配预算", __("可翻页继续查找，或核对预算年度、科目、成本中心和项目。"));
			if (offset) button(picker, "上一页预算", () => budgets(Math.max(0, offset - 50)));
			if (offset + next.rows.length < next.total_rows)
				button(picker, "下一页预算", () => budgets(offset + 50));
		} catch {
			if (id === version) {
				book = null;
				picker.empty();
				status.text(__("完整预算账簿未能核验，请重新查询并检查当前权限。"));
			}
		}
	}
	function reviewDialog(preview, payload, original = null, releaseSource = null) {
		const id = version;
		let proof = preview.proof,
			decision = null;
		delete preview.proof;
		const isRelease = !!releaseSource;
		const dialog = new frappe.ui.Dialog({
			title: __(isRelease ? "人工核对释放预占" : original ? "人工重新核对并续期" : "人工核对采购预占"),
			size: "large",
			fields: [
				{ fieldtype: "HTML", fieldname: "facts" },
				{
					fieldtype: "Check",
					fieldname: "acknowledge",
					label: __("我已核对来源、预算、精确金额和本次操作，确认提交"),
					default: 0,
				},
			],
			primary_action_label: __(isRelease ? "确认并释放" : original ? "确认续期并预占" : "确认并提交预占"),
			primary_action: async (values) => {
				if (id !== version || journal.status.busy || !proof) return;
				if (values.acknowledge !== 1 && values.acknowledge !== true)
					return frappe.msgprint(__("请核对后明确勾选人工确认。"));
				dialog.get_primary_btn().prop("disabled", true);
				try {
					let result;
					if (isRelease)
						result = await send("reservation_operations.post_release", {
							confirmation: preview.confirmation,
							proof,
							acknowledge: true,
						});
					else if (original) {
						const command = {
							original_confirmation: original.confirmation,
							original_decision: original.decision,
							renewal_confirmation: preview.confirmation,
							proof,
							acknowledge: true,
						};
						if (!decision) decision = (await send("reservation_operations.renew", command)).renewal;
						if (id !== version) {
							proof = null;
							activeDialog = null;
							dialog.hide();
							journal.reset();
							status.text(__("输入已变化，续期决定已保存，尚未提交预占。"));
							return;
						}
						result = await send("reservation_operations.post_renewed", {
							...command,
							renewal_decision: decision,
						});
					} else {
						const command = { confirmation: preview.confirmation, proof, acknowledge: true };
						if (!decision) decision = (await send("reservation.confirm", command)).decision;
						lastOriginal = {
							confirmation: preview.confirmation,
							decision,
							payload,
							expires_at: preview.expires_at,
						};
						if (id !== version) {
							proof = null;
							activeDialog = null;
							dialog.hide();
							journal.reset();
							status.text(__("输入已变化，确认决定已保存，尚未提交预占。"));
							return;
						}
						result = await send("reservation_operations.post", { ...command, decision });
					}
					proof = null;
					activeDialog = null;
					dialog.hide();
					journal.reset();
					lastOriginal = null;
					update();
					status.text(
						__(result.idempotency_replayed ? "已核验并重放原事务结果。" : "事务已完成，正在核验当前位置。"),
					);
					lastPosted = result.reservation;
					if (frappe.get_route()[0] === "hrp-budget-reservations") {
						lastPosted = null;
						await showReservation(result.reservation);
					}
				} catch {
					status.text(
						__(
							journal.status.uncertain
								? "提交结果尚未核实，请继续原操作并使用原请求重试。"
								: "本次财务操作未完成，请核对提示、来源和权限；确认成功不代表预占成功。",
						),
					);
					if (!isRelease && decision && !journal.status.uncertain) renewal.toggle(true);
				} finally {
					dialog.get_primary_btn().prop("disabled", false);
					update();
				}
			},
			onhide: () => {
				if (activeDialog !== dialog) {
					proof = null;
					return;
				}
				if (journal.status.busy || journal.status.uncertain) {
					status.text(__("上次操作仍待核实，请点继续上次操作。"));
					update();
					return;
				}
				proof = null;
				if (activeDialog === dialog) activeDialog = null;
				journal.reset();
				update();
			},
		});
		const facts = dialog.fields_dict.facts.$wrapper;
		const reviewed = isRelease ? releaseSource.source : preview.review.source;
		for (const [label, value] of [
			["采购申请", reviewed.request],
			["采购明细", `${reviewed.item_code} · ${reviewed.line}`],
			["原单精确金额", reviewed.amount],
			[
				"本次精确金额",
				`${isRelease ? preview.currency : preview.review.currency} ${isRelease ? preview.amount : preview.review.amount}`,
			],
			["核对理由", isRelease ? preview.reason : preview.review.reason],
			["执行人", frappe.session.user],
			["确认有效期至", preview.expires_at],
		])
			fact(facts, label, value);
		if (isRelease) {
			fact(facts, "预占编号", preview.reservation);
			fact(facts, "释放前未结", preview.open_before);
		}
		const key = isRelease ? releaseSource.position.key : preview.review.budget_key;
		if (original) {
			fact(facts, "原决定有效期至（站点时间）", original.expires_at || "");
			fact(facts, "续期条件", __("必须在原决定到期后重新生成核对预览；服务器将检查时间与完整证据。"));
		}
		for (const [label, value] of [
			["法人", key.company],
			["医院", key.hospital],
			["原始预算单位", key.organization_unit],
			["年度", key.fiscal_year],
			["预算分类", key.budget_subject],
			["资金来源", key.funding_source],
			["项目", key.project || ""],
			["预算期间", `${key.period_start} — ${key.period_end}`],
			["预算业务日期", isRelease ? releaseSource.budget_date : preview.review.budget_date],
		])
			fact(facts, label, value);
		const renewal = button(facts, "重新核对并续期", () => {
			if (!decision || journal.status.uncertain || journal.status.busy) return;
			const base = original || {
				confirmation: preview.confirmation,
				decision,
				expires_at: preview.expires_at,
			};
			proof = null;
			activeDialog = null;
			journal.reset();
			dialog.hide();
			startPreview(payload, base);
		}).hide();
		dialog.reviewVersion = id;
		dialog.retryOriginal = async () => {
			if (!journal.status.uncertain || journal.status.busy) return;
			try {
				const pending = journal.retry();
				update();
				const result = await pending;
				proof = null;
				activeDialog = null;
				dialog.hide();
				journal.reset();
				if (result.reservation) {
					lastOriginal = null;
					lastPosted = result.reservation;
					if (frappe.get_route()[0] === "hrp-budget-reservations") {
						lastPosted = null;
						await showReservation(result.reservation);
					}
				} else {
					lastOriginal = result.decision
						? { confirmation: preview.confirmation, decision: result.decision, payload }
						: { ...original, payload };
					status.text(__("原请求结果已核实，决定已保存，尚未预占；请重新核对后明确发起续期。"));
				}
			} catch {
				status.text(__("原请求结果仍未核实，请保留本页并继续原请求重试或由管理员核对回执。"));
			} finally {
				update();
			}
		};
		activeDialog = dialog;
		dialog.show();
		update();
	}
	async function startPreview(payload, original = null) {
		if (blocked() || activeDialog) return;
		const id = version;
		try {
			const preview = await send("reservation.prepare", payload);
			if (id !== version) {
				delete preview.proof;
				return;
			}
			reviewDialog(preview, payload, original);
		} catch {
			if (id === version) status.text(__("核对预览未生成，请检查业务日期、当前预算、来源及权限。"));
		}
	}
	function intentPayload() {
		if (
			!source ||
			!budget ||
			!allocation ||
			!controls.amount.get_value() ||
			!controls.budget_date.get_value() ||
			!controls.reason.get_value()?.trim()
		)
			return null;
		return {
			source_snapshot: source.snapshot,
			source_snapshot_digest: source.digest,
			allocation,
			budget_key: budget.key,
			currency: budget.currency,
			precision: budget.precision,
			amount: controls.amount.get_value(),
			budget_date: controls.budget_date.get_value(),
			reason: controls.reason.get_value(),
		};
	}
	function prepare() {
		if (blocked() || activeDialog) return;
		const payload = intentPayload();
		if (!payload) return frappe.msgprint(__("请选定采购明细和下达预算，并填写精确金额、业务日期及理由。"));
		startPreview(payload);
	}
	async function showReservation(name) {
		if (blocked() || activeDialog || !/^[a-f0-9]{64}$/.test(name || "")) return;
		version += 1;
		const id = version;
		results.empty();
		picker.empty();
		try {
			const item = await read(
				"medops.api.v1.finance.reservation_operations.inspect",
				{ payload: { reservation: name } },
				"POST",
			);
			if (id !== version) return;
			fact(results, "预占编号", name);
			fact(results, "采购来源", `${item.source.request} · ${item.source.item_code} · ${item.source.line}`);
			for (const [field, label] of [
				["reserved", "首笔预占"],
				["released", "累计释放"],
				["open_reservation", "当前未结预占"],
			])
				fact(results, label, `${item.position.currency} ${item.position[field]}`);
			fact(results, "当前事件序号", item.position.last_sequence);
			button(results, "打开此预占", () => frappe.set_route("hrp-budget-reservations", name));
			if (/[1-9]/.test(item.position.open_reservation))
				button(results, "核对并释放", () => releaseIntent(item));
		} catch {
			if (id === version) {
				results.empty();
				status.text(__("当前位置无法完整核验，请检查编号和当前权限。"));
			}
		}
	}
	function releaseIntent(item) {
		if (blocked() || activeDialog) return;
		const id = version;
		const dialog = new frappe.ui.Dialog({
			title: __("填写本次释放"),
			fields: [
				{ fieldtype: "Data", fieldname: "amount", label: __("精确释放金额"), reqd: 1 },
				{ fieldtype: "Small Text", fieldname: "reason", label: __("释放理由"), reqd: 1 },
			],
			primary_action_label: __("生成独立核对预览"),
			primary_action: async (values) => {
				if (id !== version || journal.status.busy) return;
				dialog.get_primary_btn().prop("disabled", true);
				try {
					const payload = {
						reservation: item.position.reservation,
						amount: values.amount,
						reason: values.reason,
					};
					const preview = await send("reservation_operations.prepare_release", payload);
					if (id !== version) {
						delete preview.proof;
						return;
					}
					activeDialog = null;
					dialog.hide();
					reviewDialog(preview, payload, null, item);
				} catch {
					status.text(__("释放预览未生成，请重新核对未结金额、来源和权限。"));
				} finally {
					dialog.get_primary_btn().prop("disabled", false);
					update();
				}
			},
			onhide: () => {
				if (activeDialog === dialog) activeDialog = null;
				update();
			},
		});
		fact(dialog.$body, "当前未结预占", `${item.position.currency} ${item.position.open_reservation}`);
		activeDialog = dialog;
		dialog.show();
		update();
	}
	button(tools, "选择采购明细", lines);
	button(tools, "选择下达预算", () => budgets());
	button(tools, "核对预占", prepare, true);
	button(tools, "续期上次已确认决定", () => {
		if (blocked() || activeDialog) return;
		const payload = intentPayload();
		if (!lastOriginal || JSON.stringify(payload) !== JSON.stringify(lastOriginal.payload))
			return frappe.msgprint(
				__("本页尚无可续期的同一已确认决定，请保留当时的来源、预算、金额、日期和理由。"),
			);
		startPreview(payload, lastOriginal);
	});
	button(tools, "查询已有预占", () => showReservation(controls.reservation.get_value()));
	const resume = button(tools, "继续上次操作", () => {
		if (activeDialog?.reviewVersion !== version && journal.status.uncertain)
			return activeDialog?.retryOriginal();
		activeDialog?.show();
	});
	button(tools, "下达与冲销", () => frappe.set_route("hrp-budget-operations"));
	wrapper.reservationsRefresh = () => {
		update();
		if (activeDialog) {
			if (journal.status.uncertain) activeDialog.show();
			return;
		}
		if (lastPosted) {
			const name = lastPosted;
			lastPosted = null;
			return showReservation(name);
		}
		const name = frappe.get_route()[1];
		if (name) return showReservation(name);
		status.text(__("先选采购明细，再选历史下达预算。确认后才预占；查询完整当前位置后可独立确认释放。"));
	};
	return wrapper.reservationsRefresh();
};
frappe.pages["hrp-budget-reservations"].on_page_show = (wrapper) => wrapper.reservationsRefresh?.();
