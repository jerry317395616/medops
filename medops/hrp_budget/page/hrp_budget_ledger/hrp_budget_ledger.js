frappe.pages["hrp-budget-ledger"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("预算账簿"), single_column: true });
	const body = $("<div class='p-3'></div>").appendTo(page.main);
	$("<p class='text-muted'></p>").text(__("按原始申报单位列账。净下达额反映预算下达及冲销。")).appendTo(body);
	const filters = $("<div class='row'></div>").appendTo(body);
	const controls = {};
	let requestId = 0;
	let busy = false;
	let current = null;
	const definitions = [
		["organization_unit", "原始单位", "Link", "HRP Organization Unit"],
		["fiscal_year", "会计年度", "Link", "Fiscal Year"],
		["currency", "币种", "Link", "Currency"],
		["from_date", "开始日期", "Date", null],
		["to_date", "截止日期", "Date", null],
	];
	const actions = $("<div class='d-flex flex-wrap mb-3'></div>").appendTo(body);
	const queryButton = $("<button class='btn btn-primary mr-2 mb-2' type='button'></button>")
		.text(__("查询完整账簿"))
		.appendTo(actions);
	const exportButton = $("<button class='btn btn-default mr-2 mb-2' type='button'></button>")
		.text(__("导出 Excel"))
		.prop("disabled", true)
		.appendTo(actions);
	const status = $("<p role='status'></p>").appendTo(body);
	const totals = $("<div class='row mb-3'></div>").appendTo(body);
	const table = $("<div class='table-responsive'></div>").appendTo(body);
	const navigation = $("<div class='d-flex align-items-center mt-3'></div>").appendTo(body);
	const previous = $("<button class='btn btn-default mr-2' type='button'></button>")
		.text(__("上一页"))
		.prop("disabled", true)
		.appendTo(navigation);
	const next = $("<button class='btn btn-default mr-2' type='button'></button>")
		.text(__("下一页"))
		.prop("disabled", true)
		.appendTo(navigation);
	const position = $("<span></span>").appendTo(navigation);

	function buttons() {
		queryButton.prop("disabled", busy);
		exportButton.prop("disabled", busy || !current);
		previous.prop("disabled", busy || !current || !current.book.offset);
		next.prop("disabled", busy || !current || current.book.next_offset === null);
	}

	function invalidate() {
		requestId += 1;
		busy = false;
		current = null;
		table.empty();
		totals.empty();
		position.text("");
		status.text(__("请选择原始单位、年度和币种；日期留空时使用整个会计年度。"));
		buttons();
	}

	for (const [fieldname, label, fieldtype, options] of definitions) {
		const parent = $("<div class='col-sm-6 col-lg-4'></div>").appendTo(filters);
		controls[fieldname] = frappe.ui.form.make_control({
			parent,
			df: { fieldname, label: __(label), fieldtype, options, onchange: invalidate },
			render_input: true,
		});
	}
	controls.currency.set_value(frappe.defaults.get_user_default("currency") || "CNY");

	function payload() {
		const query = {};
		for (const [fieldname] of definitions) {
			const value = controls[fieldname].get_value();
			if (value) query[fieldname] = value;
		}
		return query;
	}

	async function read(method, args) {
		const response = await frappe.call({
			method: `medops.api.v1.finance.ledger.${method}`,
			type: "GET",
			args,
		});
		return response.message;
	}

	async function entry(name) {
		try {
			const row = await read("get_entry", { name });
			const dialog = new frappe.ui.Dialog({
				title: __("原始分录"),
				fields: [{ fieldtype: "HTML", fieldname: "entry" }],
				size: "large",
			});
			const content = $("<dl class='row'></dl>").appendTo(dialog.fields_dict.entry.$wrapper);
			for (const [key, value] of Object.entries(row)) {
				$("<dt class='col-sm-4'></dt>")
					.text(frappe.meta.get_label("HRP Budget Ledger", key) || key)
					.appendTo(content);
				$("<dd class='col-sm-8 text-break'></dd>")
					.text(value ?? "")
					.appendTo(content);
			}
			dialog.show();
		} catch {
			invalidate();
			status.text(__("当前无法读取该分录，请重新查询并检查权限。"));
		}
	}

	function render(book) {
		const context = book.context;
		status.text(
			`${context.organization_unit_label} · ${context.fiscal_year} · ${context.currency} · ${context.from_date} — ${context.to_date}`,
		);
		totals.empty();
		for (const [key, label] of [
			["opening", "期初净下达额"],
			["debit", "期间下达额"],
			["credit", "期间冲销额"],
			["closing", "期末净下达额"],
		]) {
			const cell = $("<div class='col-sm-6 col-lg-3 mb-2'></div>").appendTo(totals);
			$("<div class='text-muted'></div>").text(__(label)).appendTo(cell);
			$("<strong></strong>").text(book.totals[key]).appendTo(cell);
		}
		table.empty();
		const grid = $("<table class='table table-bordered table-hover'></table>").appendTo(table);
		const head = $("<tr></tr>").appendTo($("<thead></thead>").appendTo(grid));
		const columns = [
			["name", "分录"],
			["posting_date", "日期"],
			["period_index", "期间"],
			["budget_subject", "预算分类"],
			["funding_source", "资金来源"],
			["account", "科目"],
			["debit", "下达额"],
			["credit", "冲销额"],
			["running_balance", "累计净下达额"],
		];
		for (const [, label] of columns) $("<th></th>").text(__(label)).appendTo(head);
		const rows = $("<tbody></tbody>").appendTo(grid);
		for (const item of book.rows) {
			const line = $("<tr></tr>").appendTo(rows);
			for (const [key] of columns) {
				const cell = $("<td></td>").appendTo(line);
				if (key === "name") {
					$("<button class='btn btn-link p-0' type='button'></button>")
						.text(item.name.slice(0, 12))
						.on("click", () => entry(item.name))
						.appendTo(cell);
				} else cell.text(item[key] ?? "");
			}
		}
		if (!book.rows.length) $("<p class='text-muted'></p>").text(__("当前范围没有分录。")).appendTo(table);
		position.text(
			`${book.offset + (book.rows.length ? 1 : 0)} — ${book.offset + book.rows.length} / ${book.total_rows}`,
		);
	}

	async function load(offset = 0) {
		const query = payload();
		if (!query.organization_unit || !query.fiscal_year || !query.currency) {
			frappe.msgprint(__("请先选择原始单位、会计年度和币种。"));
			return;
		}
		const id = ++requestId;
		const digest = offset ? current?.book.statement_digest : null;
		busy = true;
		buttons();
		status.text(__("正在核验完整账簿及当前来源权限……"));
		try {
			const book = await read("statement", {
				payload: JSON.stringify(query),
				offset,
				limit: 50,
				expected_digest: digest,
			});
			if (id !== requestId) return;
			current = { query, book };
			render(book);
		} catch {
			if (id !== requestId) return;
			invalidate();
			status.text(__("账簿未能完整读取，请检查筛选条件和权限后重新查询。"));
		} finally {
			if (id === requestId) {
				busy = false;
				buttons();
			}
		}
	}

	async function download() {
		if (!current || busy) return;
		const selected = current;
		const id = requestId;
		busy = true;
		buttons();
		try {
			const args = new URLSearchParams({
				payload: JSON.stringify(selected.query),
				expected_digest: selected.book.statement_digest,
			});
			const response = await fetch(`/api/method/medops.api.v1.finance.ledger.export_statement?${args}`, {
				credentials: "same-origin",
			});
			if (!response.ok || !(response.headers.get("Content-Type") || "").includes("spreadsheet"))
				throw new Error("Statement export was rejected");
			const blob = await response.blob();
			if (id !== requestId) return;
			const url = URL.createObjectURL(blob);
			const link = document.createElement("a");
			link.href = url;
			link.download = "budget-statement.xlsx";
			link.click();
			URL.revokeObjectURL(url);
		} catch {
			if (id === requestId) status.text(__("导出未完成；请重新查询，确认账簿和导出权限。"));
		} finally {
			if (id === requestId) {
				busy = false;
				buttons();
			}
		}
	}

	queryButton.on("click", () => load());
	previous.on("click", () => current && load(Math.max(0, current.book.offset - current.book.limit)));
	next.on("click", () => current && load(current.book.next_offset));
	exportButton.on("click", download);
	frappe.model.with_doctype("HRP Budget Ledger");
	invalidate();
};
