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
						source: sourceReview,
						budget_date: "2026-10-01",
						position: {
							reservation: root,
							key,
							currency: "CNY",
							reserved: "10.00",
							released: "0.00",
							open_reservation: "10.00",
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
			throw new Error("Unexpected method " + method);
		},
	};
	const context = { frappe, $: (html) => new Element(html), __: (text) => text };
	runInNewContext(sessionSource, context);
	runInNewContext(pageSource, context);
	await frappe.pages["hrp-budget-reservations"].on_page_load({});
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
	async function ready() {
		await controls.request.user("MR-1");
		await controls.organization_unit.user("Unit");
		await controls.fiscal_year.user("Year");
		await click("选择采购明细");
		await click("1 · ITEM-1");
		await click("选择下达预算");
		await click(/^预算分类 ·/);
		await controls.reason.user("Synthetic human review");
		await click("核对预占");
	}
	return {
		calls,
		controls,
		messages,
		ready,
		click,
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
