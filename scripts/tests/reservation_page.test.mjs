// Execute the actual page script with a small native-control/DOM protocol double.
// This tests consent and request sequencing; it does not prove rendered Desk UI.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { setImmediate } from "node:timers/promises";
import { runInNewContext } from "node:vm";
import test from "node:test";

const pageSource = readFileSync(
	new URL("../../medops/hrp_budget/page/hrp_budget_reservations/hrp_budget_reservations.js", import.meta.url),
	"utf8",
);
const sessionSource = readFileSync(
	new URL("../../medops/public/js/reservation_session.js", import.meta.url),
	"utf8",
);
const proof = "synthetic-browser-proof-" + "P".repeat(40);
const sha = (value) => value.repeat(64);
const key = {
	company: "Company",
	hospital: "Hospital",
	organization_unit: "Unit",
	fiscal_year: "Year",
	budget_subject: "Subject",
	funding_source: "Fund",
	project: null,
	period_start: "2026-01-01",
	period_end: "2026-12-31",
};
const facts = {
	...key,
	kind: "material_request_source",
	request: "MR-1",
	line: "ROW-1",
	item_code: "ITEM-1",
	currency: "CNY",
	amount: "10.000000000",
	quantity: "1.000000000",
	uom: "Unit",
	cost_center: "Center",
	expense_account: "Account",
	transaction_date: "2026-10-01",
};
const sourceReview = {
	request: facts.request,
	line: facts.line,
	item_code: facts.item_code,
	amount: facts.amount,
	cost_center: facts.cost_center,
	expense_account: facts.expense_account,
};
const root = sha("d");

async function flush() {
	for (let index = 0; index < 12; index += 1) await setImmediate();
}

async function setup(override = async () => undefined, route = ["hrp-budget-reservations"]) {
	const elements = [],
		dialogs = [],
		controls = {},
		calls = [],
		messages = [];
	class Element {
		constructor(html = "<div>") {
			this.tag = (html.match(/<([a-z]+)/) || [null, "div"])[1];
			this.children = [];
			this.visible = true;
			this.handlers = {};
			this.disabled = false;
			this.value = "";
			elements.push(this);
		}
		appendTo(parent) {
			this.parent = parent;
			parent.children.push(this);
			return this;
		}
		addClass() {
			return this;
		}
		text(value) {
			if (value === undefined) return this.value;
			this.value = value;
			return this;
		}
		on(name, handler) {
			this.handlers[name] = handler;
			return this;
		}
		prop(name, value) {
			this[name] = value;
			return this;
		}
		empty() {
			for (const child of this.children) child.detached = true;
			this.children = [];
			return this;
		}
		hide() {
			this.visible = false;
			return this;
		}
		show() {
			this.visible = true;
			return this;
		}
		toggle(value) {
			this.visible = value;
			return this;
		}
		find(selector) {
			const found = [];
			function visit(element) {
				for (const child of element.children) {
					if (selector.split(",").includes(child.tag)) found.push(child);
					visit(child);
				}
			}
			visit(this);
			return {
				prop(name, value) {
					for (const item of found) item.prop(name, value);
				},
			};
		}
	}
	class Dialog {
		constructor(config) {
			this.config = config;
			this.$body = new Element();
			this.fields_dict = {};
			this.primary = new Element("<button>");
			for (const field of config.fields)
				this.fields_dict[field.fieldname] = { $wrapper: new Element().appendTo(this.$body) };
			dialogs.push(this);
		}
		show() {
			this.visible = true;
			return this;
		}
		hide() {
			this.visible = false;
			this.config.onhide?.();
			return this;
		}
		get_primary_btn() {
			return this.primary;
		}
		async submit(acknowledge = 1, extra = {}) {
			await this.config.primary_action({ acknowledge, ...extra });
			await flush();
		}
	}
	let prepared = 0,
		random = 0;
	const frappe = {
		pages: { "hrp-budget-reservations": {} },
		require: async () => {},
		session: { user: "manager@example.invalid" },
		utils: { get_random: () => `random-${++random}` },
		get_route: () => route,
		set_route: (...next) => {
			route = next;
		},
		msgprint: (message) => messages.push(message),
		ui: {
			Dialog,
			make_app_page: () => ({ main: new Element() }),
			form: {
				make_control: ({ parent, df }) => {
					const input = new Element("<input>").appendTo(parent);
					const control = {
						$input: input,
						get_value: () => control.value || "",
						set_value: async (value) => {
							control.value = value;
							df.onchange();
						},
						user: async (value, force = false) => {
							assert.ok(force || !input.disabled, `Control ${df.fieldname} disabled`);
							control.value = value;
							input.handlers.input();
						},
					};
					controls[df.fieldname] = control;
					return control;
				},
			},
		},
		call: async (request) => {
			const clean = JSON.parse(JSON.stringify(request));
			calls.push(clean);
			const overridden = await override(clean, calls);
			if (overridden !== undefined) return { message: overridden };
			const method = request.method.replace("medops.api.v1.finance.", ""),
				payload = request.args?.payload;
			if (method === "frappe.client.get")
				return {
					message: {
						name: "MR-1",
						docstatus: 1,
						material_request_type: "Purchase",
						items: [{ idx: 1, item_code: facts.item_code, name: facts.line }],
					},
				};
			if (method === "reservation.source")
				return { message: { source_snapshot: JSON.stringify(facts), source_snapshot_digest: sha("f") } };
			if (method === "reservation_operations.recovery")
				return {
					message: {
						effect: "read_only",
						source_snapshot_digest: payload.source_snapshot_digest,
						budget_key: payload.budget_key,
						rows: [],
						total_rows: 0,
						offset: 0,
						limit: 20,
						directory_digest: sha("c"),
					},
				};
			if (method === "ledger.statement")
				return {
					message: {
						rows: [
							{
								...key,
								currency: "CNY",
								currency_precision: 2,
								account: "Account",
								cost_center: "Center",
								amount: "100.00",
								is_reversal: 0,
							},
						],
						display_labels: [{ budget_subject: "预算分类", funding_source: "资金来源" }],
						statement_digest: sha("e"),
						total_rows: 1,
						offset: 0,
					},
				};
			if (method === "reservation.prepare")
				return {
					message: {
						effect: "confirmation_only",
						confirmation: sha(String(++prepared)),
						proof,
						expires_at: "2026-10-28 12:00:00",
						review: { ...payload, source: sourceReview },
					},
				};
			if (method === "reservation.confirm")
				return { message: { effect: "confirmation_only", decision: sha("a") } };
			if (method === "reservation_operations.renew")
				return { message: { effect: "decision_renewed", renewal: sha("b") } };
			if (method === "reservation_operations.post" || method === "reservation_operations.post_renewed")
				return { message: { effect: "reservation_posted", reservation: root } };
			if (method === "reservation_operations.inspect")
				return {
					message: {
						effect: "read_only",
						source: sourceReview,
						source_digest: sha("f"),
						budget_date: "2026-10-01",
						position: {
							reservation: payload.reservation,
							key,
							currency: "CNY",
							reserved: "10.00",
							released: "0.00",
							open_reservation: "10.00",
							committed: "0.00",
							open_commitment: "0.00",
							actual: "0.00",
							last_sequence: 1,
						},
					},
				};
			if (method === "reservation_operations.prepare_release")
				return {
					message: {
						effect: "confirmation_only",
						confirmation: sha("c"),
						proof,
						expires_at: "2026-10-28 12:00:00",
						reservation: root,
						amount: "2.00",
						currency: "CNY",
						open_before: "10.00",
						reason: payload.reason,
					},
				};
			if (method === "reservation_operations.post_release")
				return { message: { effect: "reservation_released", reservation: root } };
			if (method === "reservation_operations.commitment_orders")
				return {
					message: {
						effect: "read_only",
						reservation: root,
						source_digest: sha("f"),
						orders: [
							{
								order: "PO-1",
								line: "PO-LINE-1",
								line_number: 2,
								item_code: "ITEM-1",
								currency: "CNY",
								amount: "10.00",
								supplier: "Supplier",
							},
						],
					},
				};
			if (method === "reservation_operations.prepare_commitment")
				return {
					message: {
						effect: "confirmation_only",
						reservation: root,
						confirmation: sha("c"),
						proof,
						purchase_order: "PO-1",
						purchase_order_line: "PO-LINE-1",
						currency: "CNY",
						precision: 2,
						amount: payload.amount,
						expires_at: "2026-10-29 12:00:00",
						review: {
							actor: "manager@example.invalid",
							source: sourceReview,
							order: {
								order: "PO-1",
								line: "PO-LINE-1",
								line_number: 2,
								supplier: "Supplier",
								transaction_date: "2026-10-01",
								amount: "10.00",
								item_code: "ITEM-1",
								uom: "Unit",
								quantity: "1.000000000",
							},
							budget_key: key,
							budget_subject_label: "设备预算 (EQUIPMENT)",
							funding_source_label: "自有资金 (OWN)",
							currency: "CNY",
							precision: 2,
							amount: payload.amount,
							reason: payload.reason,
							open_before: "10.00",
							order_remaining: "8.00",
							budget_date: "2026-10-01",
						},
					},
				};
			if (method === "reservation_operations.post_commitment")
				return { message: { effect: "reservation_committed", reservation: root } };
			throw new Error("Unexpected method " + method);
		},
	};
	const context = { frappe, $: (html) => new Element(html), __: (text) => text };
	runInNewContext(sessionSource, context);
	runInNewContext(pageSource, context);
	const wrapper = {};
	await frappe.pages["hrp-budget-reservations"].on_page_load(wrapper);
	await flush();
	function visible(element) {
		return element.visible && !element.detached && (!element.parent || visible(element.parent));
	}
	async function click(label) {
		const button = elements.findLast(
			(element) =>
				element.tag === "button" &&
				element.handlers.click &&
				visible(element) &&
				!element.disabled &&
				(typeof label === "string" ? element.value === label : label.test(element.value)),
		);
		assert.ok(button, `Missing enabled button ${label}`);
		await button.handlers.click();
		await flush();
	}
	async function ready(open = true) {
		await controls.request.user("MR-1");
		await controls.organization_unit.user("Unit");
		await controls.fiscal_year.user("Year");
		await click("选择采购明细");
		await click("1 · ITEM-1");
		await click("选择下达预算");
		await click(/^预算分类 ·/);
		await controls.reason.user("Synthetic human review");
		if (open) await click("核对预占");
	}
	return {
		calls,
		controls,
		messages,
		ready,
		click,
		navigate: async (next) => {
			route = next;
			await wrapper.reservationsRefresh();
			await flush();
		},
		dialog: () => dialogs.findLast((dialog) => dialog.visible),
		texts: () => elements.map((element) => element.value).join("\n"),
	};
}

test("actual page requires a separate unchecked human review and never renders proof", async () => {
	const app = await setup();
	await app.ready();
	await app.dialog().submit(0);
	assert.equal(
		app.calls.some((call) => call.method.endsWith("reservation.confirm")),
		false,
	);
	assert.equal(app.texts().includes(proof), false);
	await app.dialog().submit(1);
	assert.equal(app.calls.filter((call) => call.method.endsWith("reservation.confirm")).length, 1);
	assert.equal(app.calls.filter((call) => call.method.endsWith("reservation_operations.post")).length, 1);
});

test("unknown post repeats the same post key without confirming twice", async () => {
	let posts = 0;
	const app = await setup(async (call) => {
		if (call.method.endsWith("reservation_operations.post") && ++posts === 1) throw { status: 0 };
	});
	await app.ready();
	const dialog = app.dialog();
	await dialog.submit();
	await dialog.submit();
	const calls = app.calls.filter((call) => call.method.endsWith("reservation_operations.post"));
	assert.deepEqual(calls[0], calls[1]);
	assert.equal(app.calls.filter((call) => call.method.endsWith("reservation.confirm")).length, 1);
});

test("recovering an unknown confirmation invalidates the earlier empty history", async () => {
	let confirmations = 0;
	const app = await setup(async (call) => {
		if (call.method.endsWith("reservation.confirm") && ++confirmations === 1) throw { status: 0 };
	});
	await app.ready();
	await app.dialog().submit();
	await app.controls.amount.user("5.00", true);
	await app.click("继续上次操作");
	const confirms = app.calls.filter((call) => call.method.endsWith("reservation.confirm"));
	assert.deepEqual(confirms[0], confirms[1]);
	assert.equal(
		app.calls.some((call) => call.method.endsWith("reservation_operations.post")),
		false,
	);
	await app.click("核对预占");
	assert.equal(app.calls.filter((call) => call.method.endsWith("reservation.prepare")).length, 1);
	assert.match(app.messages.at(-1), /请先查询并恢复已有操作/);
});

test("renewal occurs only after a new explicit review of an existing decided intent", async () => {
	const app = await setup(async (call) => {
		if (call.method.endsWith("reservation_operations.post")) throw { status: 409 };
	});
	await app.ready();
	await app.dialog().submit();
	assert.equal(
		app.calls.some((call) => call.method.endsWith("reservation_operations.renew")),
		false,
	);
	await app.click("重新核对并续期");
	await app.dialog().submit(0);
	assert.equal(
		app.calls.some((call) => call.method.endsWith("reservation_operations.renew")),
		false,
	);
	await app.dialog().submit(1);
	const renewal = app.calls.find((call) => call.method.endsWith("reservation_operations.renew"));
	assert.equal(renewal.args.payload.original_confirmation, sha("1"));
	assert.equal(renewal.args.payload.original_decision, sha("a"));
	assert.equal(
		app.calls.filter((call) => call.method.endsWith("reservation_operations.post_renewed")).length,
		1,
	);
});

test("input invalidation after confirmation prevents automatic posting", async () => {
	let resolve;
	const app = await setup(async (call) => {
		if (call.method.endsWith("reservation.confirm"))
			return new Promise((done) => {
				resolve = done;
			});
	});
	await app.ready();
	const submitting = app.dialog().submit();
	await flush();
	await app.controls.amount.user("5.00", true);
	resolve({ effect: "confirmation_only", decision: sha("a") });
	await submitting;
	assert.equal(
		app.calls.some((call) => call.method.endsWith("reservation_operations.post")),
		false,
	);
});

test("input change after an unknown post can recover only that original request", async () => {
	let posts = 0;
	const app = await setup(async (call) => {
		if (call.method.endsWith("reservation_operations.post") && ++posts === 1) throw { status: 0 };
	});
	await app.ready();
	await app.dialog().submit();
	await app.controls.amount.user("5.00", true);
	await app.click("继续上次操作");
	const posted = app.calls.filter((call) => call.method.endsWith("reservation_operations.post"));
	assert.deepEqual(posted[0], posted[1]);
	assert.equal(app.calls.filter((call) => call.method.endsWith("reservation.prepare")).length, 1);
});

test("release displays exact current facts and requires an independent unchecked review", async () => {
	const app = await setup(undefined, ["hrp-budget-reservations", root]);
	await app.click("核对并释放");
	await app.dialog().submit(0, { amount: "2.00", reason: "Synthetic release" });
	assert.equal(
		app.calls.some((call) => call.method.endsWith("reservation_operations.post_release")),
		false,
	);
	assert.equal(app.texts().includes(proof), false);
	await app.dialog().submit(0);
	assert.equal(
		app.calls.some((call) => call.method.endsWith("reservation_operations.post_release")),
		false,
	);
	await app.dialog().submit(1);
	assert.equal(
		app.calls.filter((call) => call.method.endsWith("reservation_operations.post_release")).length,
		1,
	);
});

test("an explicit requested root takes priority over a remembered background result", async () => {
	let resolve;
	const app = await setup(async (call) => {
		if (call.method.endsWith("reservation_operations.post"))
			return new Promise((done) => {
				resolve = done;
			});
	});
	await app.ready();
	const pending = app.dialog().submit();
	await flush();
	await app.navigate(["another-page"]);
	resolve({ effect: "reservation_posted", reservation: root });
	await pending;
	await app.navigate(["hrp-budget-reservations", sha("e")]);
	assert.equal(
		app.calls.findLast((call) => call.method.endsWith("reservation_operations.inspect")).args.payload
			.reservation,
		sha("e"),
	);
	await app.click("查看上次完成结果");
	assert.equal(
		app.calls.findLast((call) => call.method.endsWith("reservation_operations.inspect")).args.payload
			.reservation,
		root,
	);
});

function directory(call, state = "Expired") {
	const row = {
		confirmation: sha("6"),
		decision: sha("a"),
		intent_digest: sha("f"),
		allocation: "saved-allocation",
		amount: "4.00",
		currency: "CNY",
		precision: 2,
		budget_date: "2026-10-01",
		recorded_at: "2026-10-01 10:00:00",
		expires_at: "2026-10-01 10:05:00",
		state,
		reservation: state === "Posted" ? root : null,
		intent:
			state === "Expired"
				? {
						allocation: "saved-allocation",
						amount: "4.00",
						currency: "CNY",
						precision: 2,
						budget_date: "2026-10-01",
						reason: "Previously confirmed intent",
					}
				: null,
	};
	return {
		effect: "read_only",
		source_snapshot_digest: call.args.payload.source_snapshot_digest,
		budget_key: call.args.payload.budget_key,
		rows: [row],
		total_rows: 1,
		offset: 0,
		limit: 20,
		directory_digest: sha("c"),
	};
}

test("a fresh page restores an expired decision using a new unchecked renewal review", async () => {
	const app = await setup(async (call) =>
		call.method.endsWith("reservation_operations.recovery") ? directory(call) : undefined,
	);
	await app.ready(false);
	await app.click("核对预占");
	assert.equal(app.dialog(), undefined);
	assert.equal(
		app.calls.some((call) => call.method.endsWith("reservation.prepare")),
		false,
	);
	await app.click("恢复并重新核对续期");
	assert.equal(app.controls.amount.get_value(), "4.00");
	assert.equal(app.controls.reason.get_value(), "Previously confirmed intent");
	await app.dialog().submit(0);
	assert.equal(
		app.calls.some((call) => call.method.endsWith("reservation_operations.renew")),
		false,
	);
	await app.dialog().submit(1);
	const renewed = app.calls.find((call) => call.method.endsWith("reservation_operations.renew"));
	assert.equal(renewed.args.payload.original_confirmation, sha("6"));
	assert.equal(renewed.args.payload.original_decision, sha("a"));
	assert.equal(
		app.calls.some((call) => call.method.endsWith("reservation.confirm")),
		false,
	);
	assert.equal(
		app.calls.filter((call) => call.method.endsWith("reservation_operations.post_renewed")).length,
		1,
	);
});

test("recovery rechecks a selected decision and opens a concurrently posted root without preparing", async () => {
	const app = await setup(async (call) =>
		call.method.endsWith("reservation_operations.recovery")
			? directory(call, call.args.payload.decision ? "Posted" : "Expired")
			: undefined,
	);
	await app.ready(false);
	await app.click("恢复并重新核对续期");
	assert.equal(
		app.calls.some((call) => call.method.endsWith("reservation.prepare")),
		false,
	);
	assert.equal(
		app.calls.findLast((call) => call.method.endsWith("reservation_operations.inspect")).args.payload
			.reservation,
		root,
	);
});

test("failed history verification blocks both implicit and explicit new allocation paths", async () => {
	const app = await setup(async (call) => {
		if (call.method.endsWith("reservation_operations.recovery")) throw { status: 403 };
	});
	await app.ready(false);
	await app.click("新增独立分摊");
	await app.click("核对预占");
	assert.equal(
		app.calls.some((call) => call.method.endsWith("reservation.prepare")),
		false,
	);
});

test("an existing changed-source record requires an explicit independent allocation before a new preview", async () => {
	const app = await setup(async (call) =>
		call.method.endsWith("reservation_operations.recovery") ? directory(call, "Source Changed") : undefined,
	);
	await app.ready(false);
	await app.click("核对预占");
	assert.equal(
		app.calls.some((call) => call.method.endsWith("reservation.prepare")),
		false,
	);
	await app.click("新增独立分摊");
	await app.click("核对预占");
	const prepared = app.calls.find((call) => call.method.endsWith("reservation.prepare"));
	assert.notEqual(prepared.args.payload.allocation, "saved-allocation");
	await app.dialog().submit(0);
	assert.equal(
		app.calls.some((call) => call.method.endsWith("reservation.confirm")),
		false,
	);
});

test("commitment requires native order choice and a separate unchecked review", async () => {
	const app = await setup(async () => undefined, ["hrp-budget-reservations", root]);
	await app.click("核对采购承诺");
	await app.dialog().submit(1, { amount: "2.00", reason: "Synthetic commitment" });
	assert.equal(
		app.calls.some((call) => call.method.endsWith("prepare_commitment")),
		false,
	);
	await app.click(/^PO-1 · 明细行 2/);
	await app.dialog().submit(1, { amount: "2.00", reason: "Synthetic commitment" });
	const review = app.dialog();
	assert.equal(review.config.fields.find((field) => field.fieldname === "acknowledge").default, 0);
	assert.equal(app.texts().includes("设备预算 (EQUIPMENT)"), true);
	assert.equal(app.texts().includes("订单行未承诺金额：CNY 8.00"), true);
	assert.equal(app.texts().includes(proof), false);
	await review.submit(0);
	assert.equal(
		app.calls.some((call) => call.method.endsWith("post_commitment")),
		false,
	);
	await review.submit(1);
	assert.equal(app.calls.filter((call) => call.method.endsWith("post_commitment")).length, 1);
	assert.equal(
		app.calls.some((call) => call.method.endsWith("reservation.confirm")),
		false,
	);
});

test("unknown commitment submission can only retry its original bytes and key", async () => {
	let posts = 0;
	const app = await setup(
		async (call) => {
			if (call.method.endsWith("post_commitment") && ++posts === 1) throw { status: 504 };
		},
		["hrp-budget-reservations", root],
	);
	await app.click("核对采购承诺");
	await app.click(/^PO-1 · 明细行 2/);
	await app.dialog().submit(1, { amount: "2.00", reason: "Synthetic commitment" });
	await app.dialog().submit(1);
	assert.equal(app.texts().includes("提交结果尚未核实"), true);
	await app.dialog().submit(1);
	const calls = app.calls.filter((call) => call.method.endsWith("post_commitment"));
	assert.equal(calls.length, 2);
	assert.deepEqual(calls[0].args, calls[1].args);
	assert.equal(calls[0].headers["Idempotency-Key"], calls[1].headers["Idempotency-Key"]);
	assert.equal(app.calls.filter((call) => call.method.endsWith("prepare_commitment")).length, 1);
});

test("incomplete commitment review never opens a consent dialog or posts", async () => {
	const app = await setup(
		async (call) => {
			if (call.method.endsWith("prepare_commitment"))
				return {
					effect: "confirmation_only",
					confirmation: sha("c"),
					proof,
					reservation: root,
					purchase_order: "PO-1",
					purchase_order_line: "PO-LINE-1",
					review: { order: { order: "PO-1", line: "PO-LINE-1" } },
				};
		},
		["hrp-budget-reservations", root],
	);
	await app.click("核对采购承诺");
	await app.click(/^PO-1 · 明细行 2/);
	await app.dialog().submit(1, { amount: "2.00", reason: "Synthetic commitment" });
	assert.equal(
		app.dialog().config.fields.some((field) => field.fieldname === "acknowledge"),
		false,
	);
	assert.equal(
		app.calls.some((call) => call.method.endsWith("post_commitment")),
		false,
	);
	assert.equal(app.texts().includes(proof), false);
});

async function expiredPreviewRecovery(kind) {
	const previewMethod = `prepare_${kind}`;
	const postMethod = `post_${kind}`;
	const attempts = [];
	let expiredKey;
	const app = await setup(
		async (call) => {
			if (!call.method.endsWith(previewMethod)) return;
			attempts.push(call);
			const requestKey = call.headers["Idempotency-Key"];
			if (!expiredKey) {
				expiredKey = requestKey;
				// The preview was saved but its response was lost; a later retry
				// reaches that same, now-expired confirmation and is rejected.
				throw { status: 504 };
			}
			if (requestKey === expiredKey) throw { status: 403 };
		},
		["hrp-budget-reservations", root],
	);
	const openInput = async () => {
		await app.click(kind === "commitment" ? "核对采购承诺" : "核对并释放");
		if (kind === "commitment") await app.click(/^PO-1 · 明细行 2/);
	};
	const input = { amount: "2.00", reason: "Same reviewed intent after preview expiry" };
	await openInput();
	await app.dialog().submit(0, input);
	await app.dialog().submit(0, input);
	assert.equal(attempts.length, 2);
	assert.equal(attempts[0].headers["Idempotency-Key"], attempts[1].headers["Idempotency-Key"]);
	assert.equal(
		app.dialog().config.fields.some((field) => field.fieldname === "acknowledge"),
		false,
	);
	app.dialog().hide();
	await openInput();
	await app.dialog().submit(0, input);
	assert.equal(attempts.length, 3);
	assert.notEqual(attempts[2].headers["Idempotency-Key"], expiredKey);
	assert.deepEqual(attempts[2].args, attempts[0].args);
	assert.equal(app.dialog().config.fields.find((field) => field.fieldname === "acknowledge").default, 0);
	assert.equal(
		app.calls.some((call) => call.method.endsWith(postMethod)),
		false,
	);
}

test("closing a failed expired commitment preview allows a new unchecked preview for the same intent", async () => {
	await expiredPreviewRecovery("commitment");
});

test("closing a failed expired release preview allows a new unchecked preview for the same intent", async () => {
	await expiredPreviewRecovery("release");
});
