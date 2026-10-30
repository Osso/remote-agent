import { appendFileSync, chmodSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const identifier = /^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}$/;
const stopReasons = new Set(["stop", "length", "toolUse", "error", "aborted"]);

export function createTimingRecorder({ writeRecord, now }) {
	let request;
	let index = 0;
	const emit = (stage, fields = {}) =>
		writeRecord({ stage, ...request.identity, ...fields });
	const elapsed = () => now() - request.started;
	return {
		start(ctx, sessionId) {
			const identity = { pid: process.pid, sessionId, requestIndex: ++index };
			for (const [key, value] of [
				["provider", ctx.model?.provider],
				["model", ctx.model?.id],
				["effort", ctx.getThinkingLevel?.()],
			]) {
				if (typeof value === "string" && identifier.test(value))
					identity[key] = value;
			}
			request = {
				identity,
				started: now(),
				responseCount: 0,
				thinkingChars: 0,
				textChars: 0,
				toolcallChars: 0,
			};
			emit("start");
		},
		context(event) {
			if (request) request.contextMessageCount = event.messages.length;
		},
		prepared() {
			if (request)
				emit("prepared", {
					localPreparedMsFromStart: elapsed(),
					...(request.contextMessageCount === undefined
						? {}
						: { contextMessageCount: request.contextMessageCount }),
				});
		},
		headers(event) {
			if (!request) return;
			request.responseCount++;
			const fields = {
				headersMsFromStart: elapsed(),
				responseCount: request.responseCount,
			};
			if (
				Number.isInteger(event.status) &&
				event.status >= 100 &&
				event.status <= 599
			)
				fields.status = event.status;
			emit("headers", fields);
		},
		messageStart(event) {
			if (
				request &&
				event.message.role === "assistant" &&
				!request.streamSeen
			) {
				request.streamSeen = true;
				emit("first_stream", { firstStreamMsFromStart: elapsed() });
			}
		},
		update(event) {
			if (!request || event.message.role !== "assistant") return;
			const delta = event.assistantMessageEvent;
			if (typeof delta.delta !== "string" || delta.delta.length === 0) return;
			if (delta.type === "thinking_delta") {
				request.thinkingChars += delta.delta.length;
				if (!request.thinkingSeen) {
					request.thinkingSeen = true;
					emit("first_thinking", { firstThinkingMsFromStart: elapsed() });
				}
			} else if (
				delta.type === "text_delta" ||
				delta.type === "toolcall_delta"
			) {
				request[delta.type === "text_delta" ? "textChars" : "toolcallChars"] +=
					delta.delta.length;
				if (!request.outputSeen) {
					request.outputSeen = true;
					emit("first_output", { firstOutputMsFromStart: elapsed() });
				}
			}
		},
		messageEnd(event) {
			if (!request || event.message.role !== "assistant") return;
			const message = event.message;
			request.usage = {};
			for (const key of ["input", "output", "cacheRead", "cacheWrite"]) {
				const value = message.usage?.[key];
				if (typeof value === "number" && Number.isFinite(value) && value >= 0)
					request.usage[key] = value;
			}
			if (stopReasons.has(message.stopReason))
				request.stopReason = message.stopReason;
		},
		end() {
			if (!request) return;
			const fields = {
				totalMsFromStart: elapsed(),
				responseCount: request.responseCount,
				thinkingChars: request.thinkingChars,
				textChars: request.textChars,
				toolcallChars: request.toolcallChars,
			};
			if (request.contextMessageCount !== undefined)
				fields.contextMessageCount = request.contextMessageCount;
			if (request.usage) Object.assign(fields, request.usage);
			if (request.stopReason) fields.stopReason = request.stopReason;
			emit("end", fields);
			request = undefined;
		},
	};
}

export default function (pi) {
	let warned = false;
	const warn = () => {
		if (!warned) {
			warned = true;
			process.stderr.write("request-timings: recording unavailable\n");
		}
	};
	let target;
	let logPath;
	try {
		const config = JSON.parse(
			readFileSync(
				join(
					dirname(fileURLToPath(import.meta.url)),
					"request-timings-target.json",
				),
				"utf8",
			),
		);
		if (
			typeof config.sessionId !== "string" ||
			!uuid.test(config.sessionId) ||
			!process.env.PI_CODING_AGENT_DIR
		)
			throw new Error();
		target = config.sessionId;
		logPath = join(
			process.env.PI_CODING_AGENT_DIR,
			"logs",
			"request-timings.jsonl",
		);
	} catch {
		warn();
		return;
	}
	const writeRecord = (record) => {
		try {
			mkdirSync(dirname(logPath), { recursive: true, mode: 0o700 });
			chmodSync(dirname(logPath), 0o700);
			const stampedRecord = { timestampMs: Date.now(), ...record };
			appendFileSync(logPath, `${JSON.stringify(stampedRecord)}\n`, {
				mode: 0o600,
			});
			chmodSync(logPath, 0o600);
		} catch {
			warn();
		}
	};
	const recorder = createTimingRecorder({
		writeRecord,
		now: () => performance.now(),
	});
	const selected = (ctx) => ctx.sessionManager.getSessionId() === target;
	pi.on("session_start", (_event, ctx) => {
		if (selected(ctx))
			writeRecord({
				stage: "session_start",
				pid: process.pid,
				sessionId: target,
			});
	});
	for (const [event, method] of [
		["model_request_start", "start"],
		["context", "context"],
		["before_provider_request", "prepared"],
		["after_provider_response", "headers"],
		["message_start", "messageStart"],
		["message_update", "update"],
		["message_end", "messageEnd"],
		["model_request_end", "end"],
	]) {
		pi.on(event, (value, ctx) => {
			if (!selected(ctx)) return;
			if (method === "start") recorder.start(ctx, target);
			else recorder[method](value);
		});
	}
}
