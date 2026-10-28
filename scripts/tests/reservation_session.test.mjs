import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { inspect } from "node:util";
import { runInNewContext } from "node:vm";
import test from "node:test";

const source = readFileSync(
	new URL("../../medops/public/js/reservation_session.js", import.meta.url),
	"utf8",
);
const proof = "private-session-proof-" + "X".repeat(40);
const command = { confirmation: "a".repeat(64), proof, acknowledge: true };
const completed = { effect: "reservation_released", reservation: "b".repeat(64) };
function session(transport) {
	const context = { frappe: {} };
	runInNewContext(source, context);
	let sequence = 0;
	return new context.frappe.medops.ReservationSession(transport, () => `key-${++sequence}`);
}

test("explicit true and a supported method are required before any request", async () => {
	let calls = 0;
	const journal = session(async () => {
		calls += 1;
		return completed;
	});
	for (const acknowledge of [false, 0, 1, "true", null])
		await assert.rejects(journal.send("reservation_operations.post_release", { ...command, acknowledge }));
	await assert.rejects(journal.send("frappe.client.insert", command));
	assert.equal(calls, 0);
});

test("only one request runs at a time and reset cannot discard an in-flight request", async () => {
	let resolve;
	const journal = session(
		() =>
			new Promise((done) => {
				resolve = done;
			}),
	);
	const first = journal.send("reservation_operations.post_release", command);
	assert.equal(journal.status.busy, true);
	await assert.rejects(journal.send("reservation_operations.post_release", command));
	assert.throws(() => journal.reset());
	resolve(completed);
	await first;
	assert.equal(journal.status.busy, false);
});

test("an unknown result retains exact bytes and key across input invalidation", async () => {
	const calls = [];
	const journal = session(async (method, payload, key) => {
		calls.push({ method, payload, key });
		if (calls.length === 1) throw new Error("unsafe transport " + proof);
		return completed;
	});
	await assert.rejects(
		journal.send("reservation_operations.post_release", command),
		(error) => !String(error).includes(proof),
	);
	assert.equal(journal.status.uncertain, true);
	journal.invalidate();
	assert.throws(() => journal.reset());
	await assert.rejects(
		journal.send("reservation_operations.post_release", { ...command, confirmation: "c".repeat(64) }),
	);
	await journal.retry();
	assert.deepEqual(calls[0], calls[1]);
	assert.equal(journal.status.uncertain, false);
	assert.equal(JSON.stringify(journal).includes(proof), false);
	assert.equal(inspect(journal).includes(proof), false);
	assert.equal(JSON.stringify(journal.status).includes(proof), false);
});

test("known rejection allows same-input retry with the original key and equivalent ordering", async () => {
	const keys = [];
	const journal = session(async (_method, _payload, key) => {
		keys.push(key);
		if (keys.length === 1) throw { status: 409, private: proof };
		return completed;
	});
	await assert.rejects(journal.send("reservation_operations.post_release", command));
	assert.equal(journal.status.uncertain, false);
	await journal.send("reservation_operations.post_release", {
		acknowledge: true,
		proof,
		confirmation: command.confirmation,
	});
	assert.equal(keys[0], keys[1]);
	journal.reset();
	await journal.send("reservation_operations.post_release", command);
	assert.notEqual(keys[1], keys[2]);
});

test("malformed financial success is unresolved and never advances to another command", async () => {
	let calls = 0;
	const journal = session(async () => (++calls === 1 ? { effect: "confirmation_only" } : completed));
	await assert.rejects(journal.send("reservation_operations.post_release", command));
	assert.equal(journal.status.uncertain, true);
	await assert.rejects(journal.send("reservation.confirm", command));
	await journal.retry();
	assert.equal(calls, 2);
});

test("a later permission rejection cannot prove an earlier unknown request rolled back", async () => {
	let calls = 0;
	const journal = session(async () => {
		calls += 1;
		if (calls === 1) throw { status: 0 };
		if (calls === 2) throw { status: 403 };
		return completed;
	});
	await assert.rejects(journal.send("reservation_operations.post_release", command));
	await assert.rejects(journal.retry());
	assert.equal(journal.status.uncertain, true);
	assert.throws(() => journal.reset());
	await journal.retry();
	assert.equal(journal.status.uncertain, false);
});

test("abandoned previews invalidate the page generation without creating financial uncertainty", async () => {
	const journal = session(async () => {
		throw new Error("private " + proof);
	});
	const before = journal.status.version;
	await assert.rejects(journal.send("reservation.prepare", {}));
	journal.invalidate();
	assert.equal(journal.status.version, before + 1);
	assert.equal(journal.status.uncertain, false);
	journal.reset();
});
