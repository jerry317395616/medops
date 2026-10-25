/* global __ENV */

import http from "k6/http";
import { check, fail } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";

const registry = JSON.parse(open("../config/performance_baselines.json"));
const scenarioId = __ENV.IONE_PERF_SCENARIO || "platform-module-registry-read";
const profileName = __ENV.IONE_PERF_PROFILE || "smoke";
const scenario = registry.scenarios.find((candidate) => candidate.scenario_id === scenarioId);

if (!scenario) {
	throw new Error("Unknown source-controlled performance scenario.");
}

const profile = scenario.profiles.find((candidate) => candidate.profile === profileName);

if (!profile) {
	throw new Error("Unknown source-controlled performance profile.");
}

const scenarioRequests = new Counter("ione_scenario_requests");
const scenarioFailures = new Rate("ione_scenario_failed");
const scenarioChecks = new Rate("ione_scenario_check");
const scenarioDuration = new Trend("ione_scenario_duration", true);

export const options = {
	discardResponseBodies: false,
	summaryTrendStats: ["avg", "min", "med", "max", "p(90)", "p(95)", "p(99)"],
	scenarios: {
		bounded_read_baseline: {
			executor: "shared-iterations",
			vus: profile.virtual_users,
			iterations: profile.iterations,
			maxDuration: `${profile.max_duration_seconds}s`,
		},
	},
	thresholds: {
		ione_scenario_requests: [
			`count==${profile.iterations}`,
			`rate>=${profile.thresholds.min_requests_per_second}`,
		],
		ione_scenario_failed: [`rate<=${profile.thresholds.max_error_rate}`],
		ione_scenario_check: [`rate>=${profile.thresholds.min_check_rate}`],
		ione_scenario_duration: [`p(95)<=${profile.thresholds.p95_ms}`, `p(99)<=${profile.thresholds.p99_ms}`],
	},
};

function requiredEnvironment(name) {
	const value = __ENV[name];
	if (!value) {
		fail(`Missing required execution environment variable: ${name}`);
	}
	return value;
}

function validateBaseUrl(value) {
	if (value.includes("?") || value.includes("#") || /:\/\/[^/@]+@/.test(value)) {
		fail("Performance target URL must not include credentials, query, or fragment.");
	}
	if (/^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/i.test(value)) {
		return value;
	}
	if (!/^https:\/\/[A-Za-z0-9.-]+(?::\d+)?$/i.test(value)) {
		fail("Performance target must use HTTPS, except for loopback development targets.");
	}
	return value;
}

function requestHeaders() {
	return {
		Authorization: `token ${requiredEnvironment("IONE_PERF_API_KEY")}:${requiredEnvironment(
			"IONE_PERF_API_SECRET",
		)}`,
		"X-Correlation-ID": requiredEnvironment("IONE_PERF_RUN_ID"),
		"User-Agent": "I-ONE-HRP-Performance/1",
	};
}

function validateModuleRegistry(response) {
	try {
		const payload = response.json();
		return (
			Array.isArray(payload.message) &&
			payload.message.length === 36 &&
			payload.message.every(
				(item) =>
					typeof item.module === "string" &&
					typeof item.module_key === "string" &&
					typeof item.enabled === "boolean",
			)
		);
	} catch {
		return false;
	}
}

const responseContracts = {
	module_registry_v1: validateModuleRegistry,
};

export function setup() {
	if (requiredEnvironment("IONE_PERF_CONFIRM") !== "NON_PRODUCTION_LOAD_TEST") {
		fail("Explicit non-production load-test confirmation is required.");
	}
	const baseUrl = validateBaseUrl(requiredEnvironment("IONE_PERF_BASE_URL"));
	const expectedSha = requiredEnvironment("IONE_PERF_REGISTRY_SHA256");
	const contractPath = "/api/method/medops.api.v1.performance.get_performance_baseline_contract";
	const response = http.get(`${baseUrl}${contractPath}`, {
		headers: requestHeaders(),
		redirects: 0,
		timeout: "10s",
		tags: { phase: "contract" },
	});
	let contract;
	try {
		contract = response.json().message;
	} catch {
		fail("Performance contract did not return valid JSON.");
	}
	const allowed = check(response, {
		"performance contract is authorized": (result) => result.status === 200,
		"performance registry matches local source": () => contract?.sha256 === expectedSha,
		"target environment is explicitly safe": () =>
			contract?.load_test_available === true &&
			contract?.environment?.managed === true &&
			registry.policy.allowed_profiles.includes(contract?.environment?.name),
		"contract forbids HTTP writes": () => contract?.execution_policy?.http_write_enabled === false,
	});
	if (!allowed) {
		fail("Target rejected the governed non-production performance contract.");
	}
	return { ready: true };
}

export default function (data) {
	if (!data?.ready) {
		fail("Performance setup did not complete.");
	}
	const baseUrl = validateBaseUrl(requiredEnvironment("IONE_PERF_BASE_URL"));
	const validator = responseContracts[scenario.response_contract];
	if (!validator || scenario.method !== "GET" || scenario.read_only !== true) {
		fail("Scenario is not a supported read-only contract.");
	}
	const response = http.get(`${baseUrl}${scenario.path}`, {
		headers: requestHeaders(),
		redirects: 0,
		timeout: "10s",
		tags: { phase: "scenario", scenario: scenario.scenario_id },
	});
	const passed = check(response, {
		"scenario status is 200": (result) => result.status === 200,
		"scenario response contract matches": validator,
	});
	scenarioRequests.add(1);
	scenarioFailures.add(!passed);
	scenarioChecks.add(passed);
	scenarioDuration.add(response.timings.duration);
}

function metricValue(data, metricName, valueName, fallback = 0) {
	return data.metrics[metricName]?.values?.[valueName] ?? fallback;
}

export function handleSummary(data) {
	const summaryPath = requiredEnvironment("IONE_PERF_SUMMARY_PATH");
	const summary = {
		schema_version: 1,
		scenario_id: scenario.scenario_id,
		scenario_version: scenario.version,
		profile: profile.profile,
		registry_sha256: requiredEnvironment("IONE_PERF_REGISTRY_SHA256"),
		tool_version: registry.policy.k6_version,
		run_id: requiredEnvironment("IONE_PERF_RUN_ID"),
		metrics: {
			request_count: metricValue(data, "ione_scenario_requests", "count"),
			error_rate: metricValue(data, "ione_scenario_failed", "rate", 1),
			check_rate: metricValue(data, "ione_scenario_check", "rate"),
			requests_per_second: metricValue(data, "ione_scenario_requests", "rate"),
			p95_ms: metricValue(data, "ione_scenario_duration", "p(95)"),
			p99_ms: metricValue(data, "ione_scenario_duration", "p(99)"),
			duration_ms: data.state?.testRunDurationMs ?? 0,
		},
	};
	return {
		stdout: `${JSON.stringify(summary, null, 2)}\n`,
		[summaryPath]: `${JSON.stringify(summary, null, 2)}\n`,
	};
}
