import assert from "node:assert/strict";
import {
	copyFileSync,
	mkdtempSync,
	readFileSync,
	rmSync,
	statSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { test } from "node:test";

const source = join(
	dirname(fileURLToPath(import.meta.url)),
	"../sandbox/openrouter-runtime/request-timings.js",
);
const target = "12345678-1234-4123-8123-123456789abc";
const ctx = (id = target) => ({
	sessionManager: { getSessionId: () => id },
	model: { provider: "openrouter", id: "vendor/model" },
	getThinkingLevel: () => "high",
});

test("records only selected session, bounded stages and safe counts", async () => {
	const temp = mkdtempSync(join(tmpdir(), "request-timings-"));
	const oldDir = process.env.PI_CODING_AGENT_DIR;
	try {
		const modulePath = join(temp, "request-timings.js");
		copyFileSync(source, modulePath);
		writeFileSync(join(temp, "package.json"), '{"type":"module"}');
		writeFileSync(
			join(temp, "request-timings-target.json"),
			JSON.stringify({ sessionId: target }),
		);
		process.env.PI_CODING_AGENT_DIR = join(temp, "agent");
		const { default: extension, createTimingRecorder } = await import(
			pathToFileURL(modulePath).href
		);
		const handlers = new Map();
		extension({ on: (name, handler) => handlers.set(name, handler) });
		const fire = (name, event = {}, context = ctx()) =>
			assert.equal(handlers.get(name)(event, context), undefined);
		fire("session_start", {}, ctx("other"));
		fire("model_request_start", {}, ctx("other"));
		fire("session_start");
		fire("model_request_start");
		fire("context", { messages: [{ secret: "SENTINEL_CONTEXT" }] });
		fire("before_provider_request", {
			payload: { secret: "SENTINEL_PAYLOAD" },
		});
		fire("after_provider_response", {
			status: 200,
			headers: { authorization: "SENTINEL_HEADER" },
		});
		fire("message_start", {
			message: { role: "assistant", content: "SENTINEL_MESSAGE" },
		});
		for (const [type, delta] of [
			["thinking_delta", "SENTINEL_THINK"],
			["text_delta", "SENTINEL_TEXT"],
			["toolcall_delta", "SENTINEL_TOOL"],
		]) {
			fire("message_update", {
				message: { role: "assistant" },
				assistantMessageEvent: { type, delta },
			});
		}
		fire("message_end", {
			message: {
				role: "assistant",
				usage: { input: 10, output: 4, cacheRead: 2, cacheWrite: 1 },
				stopReason: "stop",
				errorMessage: "SENTINEL_ERROR",
			},
		});
		fire("model_request_end");
		fire("model_request_start");
		fire("model_request_end");
		const path = join(temp, "agent/logs/request-timings.jsonl");
		const text = readFileSync(path, "utf8");
		assert.doesNotMatch(text, /SENTINEL/);
		const records = text.trim().split("\n").map(JSON.parse);
		assert.deepEqual(
			records.map((r) => r.stage),
			[
				"session_start",
				"start",
				"prepared",
				"headers",
				"first_stream",
				"first_thinking",
				"first_output",
				"end",
				"start",
				"end",
			],
		);
		assert.equal(records[2].contextMessageCount, 1);
		assert.equal(records[3].status, 200);
		assert.deepEqual(
			[
				records[7].input,
				records[7].output,
				records[7].cacheRead,
				records[7].cacheWrite,
				records[7].thinkingChars,
				records[7].textChars,
				records[7].toolcallChars,
			],
			[10, 4, 2, 1, 14, 13, 13],
		);
		assert.equal(records[7].stopReason, "stop");
		assert.equal(records[9].requestIndex, 2);
		assert.equal(records[9].input, undefined);
		assert.equal(records[9].contextMessageCount, undefined);
		assert.equal(statSync(dirname(path)).mode & 0o777, 0o700);
		assert.equal(statSync(path).mode & 0o777, 0o600);
		let time = 0;
		const deterministic = [];
		const recorder = createTimingRecorder({
			writeRecord: (record) => deterministic.push(record),
			now: () => time,
		});
		recorder.start(ctx(), target);
		time = 2;
		recorder.context({ messages: [1, 2] });
		recorder.prepared();
		time = 5;
		recorder.headers({ status: 503 });
		time = 9;
		recorder.messageStart({ message: { role: "assistant" } });
		time = 12;
		recorder.update({
			message: { role: "assistant" },
			assistantMessageEvent: { type: "thinking_delta", delta: "x" },
		});
		time = 14;
		recorder.update({
			message: { role: "assistant" },
			assistantMessageEvent: { type: "text_delta", delta: "xy" },
		});
		time = 20;
		recorder.end();
		assert.deepEqual(
			deterministic
				.slice(1)
				.map(
					(r) =>
						r.localPreparedMsFromStart ??
						r.headersMsFromStart ??
						r.firstStreamMsFromStart ??
						r.firstThinkingMsFromStart ??
						r.firstOutputMsFromStart ??
						r.totalMsFromStart,
				),
			[2, 5, 9, 12, 14, 20],
		);
	} finally {
		if (oldDir === undefined) delete process.env.PI_CODING_AGENT_DIR;
		else process.env.PI_CODING_AGENT_DIR = oldDir;
		rmSync(temp, { recursive: true, force: true });
	}
});
