/* A page-local command journal. No proof or request body is an enumerable field. */
frappe.medops ??= {};
frappe.medops.ReservationSession = class ReservationSession {
	#transport;
	#newKey;
	#commands = new Map();
	#pending = null;
	#busy = false;
	#version = 0;
	constructor(transport, newKey) {
		this.#transport = transport;
		this.#newKey = newKey;
	}
	get status() {
		return { busy: this.#busy, uncertain: !!this.#pending, version: this.#version };
	}
	invalidate() {
		this.#version += 1;
		if (!this.#busy && !this.#pending) this.#commands.clear();
		return this.#version;
	}
	reset() {
		if (this.#busy || this.#pending) throw new Error("reservation_result_unresolved");
		this.#commands.clear();
		this.#version += 1;
	}
	async retry() {
		if (!this.#pending) throw new Error("reservation_retry_unavailable");
		const { method, serialized } = this.#pending;
		return this.send(method, JSON.parse(serialized));
	}
	async send(method, payload) {
		const rules = {
			"reservation.prepare": ["confirmation_only", "confirmation", false, true],
			"reservation.confirm": ["confirmation_only", "decision", true, false],
			"reservation_operations.post": ["reservation_posted", "reservation", true, false],
			"reservation_operations.renew": ["decision_renewed", "renewal", true, false],
			"reservation_operations.post_renewed": ["reservation_posted", "reservation", true, false],
			"reservation_operations.prepare_release": ["confirmation_only", "confirmation", false, true],
			"reservation_operations.post_release": ["reservation_released", "reservation", true, false],
			"reservation_operations.prepare_commitment": ["confirmation_only", "confirmation", false, true],
			"reservation_operations.post_commitment": ["reservation_committed", "reservation", true, false],
		};
		if (!Object.hasOwn(rules, method) || this.#busy) throw new Error("reservation_command_unavailable");
		const [effect, identity, human, preview] = rules[method];
		if (human && payload?.acknowledge !== true) throw new Error("reservation_acknowledgement_required");
		// Canonical property ordering keeps equivalent retry objects on one key.
		const canonical = (value) => {
			if (Array.isArray(value)) return value.map(canonical);
			if (value && typeof value === "object")
				return Object.fromEntries(
					Object.keys(value)
						.sort()
						.map((key) => [key, canonical(value[key])]),
				);
			return value;
		};
		const serialized = JSON.stringify(canonical(payload));
		const signature = method + "\n" + serialized;
		if (this.#pending && this.#pending.signature !== signature)
			throw new Error("reservation_result_unresolved");
		let command = this.#commands.get(signature);
		if (!command) {
			command = { method, serialized, signature, key: this.#newKey() };
			this.#commands.set(signature, command);
		}
		this.#busy = true;
		try {
			const result = await this.#transport(method, JSON.parse(serialized), command.key);
			if (
				result?.effect !== effect ||
				!/^[a-f0-9]{64}$/.test(result?.[identity] || "") ||
				(preview && (typeof result.proof !== "string" || result.proof.length < 32))
			)
				throw new Error("reservation_response_incomplete");
			this.#pending = null;
			return result;
		} catch (error) {
			// A gateway/network failure or malformed success cannot establish the
			// financial outcome. Only the exact original command may then retry.
			const status = typeof error?.status === "number" && Number.isFinite(error.status) ? error.status : 0;
			if (human && (this.#pending || !status || status >= 500)) this.#pending = command;
			else this.#pending = null;
			// Do not propagate transport objects, response bodies or proof-bearing args.
			// eslint-disable-next-line preserve-caught-error -- The raw transport cause can contain the human proof.
			throw new Error("reservation_request_incomplete");
		} finally {
			this.#busy = false;
		}
	}
};
