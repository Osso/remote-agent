import { appendFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import {
	createAssistantMessageEventStream,
	type Api,
	type AssistantMessage,
	type Context,
	type Model,
	type SimpleStreamOptions,
} from "@earendil-works/pi-ai/compat";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const directory = process.env.PI_PERIODIC_TEST_DIRECTORY;
if (!directory)
	throw new Error("Missing private periodic runtime fixture directory");
const root = directory;
const report = "PERIODIC_RUNTIME_CHILD_REPORT";

function record(kind: string, details: object = {}): void {
	appendFileSync(
		join(root, "events.jsonl"),
		`${JSON.stringify({ kind, ...details })}\n`,
	);
}

async function waitForRelease(
	name: string,
	signal?: AbortSignal,
): Promise<void> {
	const deadline = Date.now() + 30_000;
	while (!existsSync(join(root, name))) {
		if (signal?.aborted) throw new Error(`Aborted ${name}`);
		if (Date.now() > deadline) throw new Error(`Timed out waiting for ${name}`);
		await new Promise((resolve) => setTimeout(resolve, 20));
	}
}

function userText(context: Context): string {
	return context.messages
		.filter((message) => message.role === "user")
		.map((message) =>
			typeof message.content === "string"
				? message.content
				: message.content
						.map((part) => (part.type === "text" ? part.text : ""))
						.join("\n"),
		)
		.join("\n");
}

function toolCall(name: string, args: object) {
	return {
		type: "toolCall" as const,
		id: `${name}-${Date.now()}`,
		name,
		arguments: args,
	};
}

async function respond(
	context: Context,
	options?: SimpleStreamOptions,
): Promise<AssistantMessage["content"]> {
	const text = userText(context);
	const results = context.messages.filter(
		(message) => message.role === "toolResult",
	);
	if (text.includes("PERIODIC_RUNTIME_MAIN")) {
		if (results.some((message) => message.toolName === "end_turn")) {
			return [
				toolCall("end_turn", {
					reason: "Mailbox report received; test complete",
				}),
			];
		}
		if (
			text.includes("HOLD_TOOL") &&
			!results.some((message) => message.toolName === "periodic_test_hold")
		) {
			return [toolCall("periodic_test_hold", {})];
		}
		if (!results.some((message) => message.toolName === "periodic_test_hold")) {
			record("main_model_held");
			await waitForRelease("release-main", options?.signal);
			record("main_model_released");
		}
		return [
			{ type: "text", text: "PERIODIC_RUNTIME_MAIN_FINISHED" },
			toolCall("end_turn", { reason: "Main completed normally" }),
		];
	}
	if (results.some((message) => message.toolName === "send_agent_message")) {
		record("child_model_held");
		await waitForRelease("release-child", options?.signal);
		return [toolCall("end_turn", { reason: "Child report delivered" })];
	}
	const parent = /toSessionId\s+("[^"]+")/.exec(text);
	if (!parent)
		throw new Error(`Child prompt lacks originating session routing: ${text}`);
	record("child_provider_request", { prompt: text });
	return [
		{ type: "text", text: report },
		toolCall("send_agent_message", {
			toAgentId: "main",
			toSessionId: JSON.parse(parent[1]),
		}),
	];
}

function stream(
	model: Model<Api>,
	context: Context,
	options?: SimpleStreamOptions,
) {
	const events = createAssistantMessageEventStream();
	const output: AssistantMessage = {
		role: "assistant",
		content: [],
		api: model.api,
		provider: model.provider,
		model: model.id,
		usage: {
			input: 0,
			output: 0,
			cacheRead: 0,
			cacheWrite: 0,
			totalTokens: 0,
			cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
		},
		stopReason: "toolUse",
		timestamp: Date.now(),
	};
	void (async () => {
		events.push({ type: "start", partial: output });
		try {
			output.content = await respond(context, options);
			for (const [contentIndex, part] of output.content.entries()) {
				if (part.type === "text") {
					events.push({ type: "text_start", contentIndex, partial: output });
					events.push({
						type: "text_delta",
						contentIndex,
						delta: part.text,
						partial: output,
					});
					events.push({
						type: "text_end",
						contentIndex,
						content: part.text,
						partial: output,
					});
				} else if (part.type === "toolCall") {
					events.push({
						type: "toolcall_start",
						contentIndex,
						partial: output,
					});
					events.push({
						type: "toolcall_end",
						contentIndex,
						toolCall: part,
						partial: output,
					});
				}
			}
			events.push({ type: "done", reason: "toolUse", message: output });
		} catch (error) {
			output.stopReason = options?.signal?.aborted ? "aborted" : "error";
			output.errorMessage = String(error);
			record("provider_error", { error: output.errorMessage });
			events.push({ type: "error", reason: output.stopReason, error: output });
		}
		events.end();
	})();
	return events;
}

export default function registerPeriodicRuntimeProvider(
	pi: ExtensionAPI,
): void {
	pi.registerProvider("periodic-runtime-faux", {
		api: "periodic-runtime-faux",
		baseUrl: "http://127.0.0.1:1/never-contacted",
		apiKey: "not-a-real-token",
		models: [
			{
				id: "periodic-runtime-faux-1",
				name: "periodic-runtime-faux-1",
				reasoning: false,
				input: ["text"],
				cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
				contextWindow: 128000,
				maxTokens: 4096,
			},
		],
		streamSimple: stream,
	});
	pi.registerTool({
		name: "periodic_test_hold",
		label: "Periodic test hold",
		description: "Controlled test-only active tool",
		parameters: Type.Object({}),
		async execute(_id, _params, signal) {
			record("main_tool_held");
			await waitForRelease("release-main", signal);
			record("main_tool_released");
			return {
				content: [{ type: "text", text: "Held tool finished normally" }],
				details: undefined,
			};
		},
	});
	pi.on("session_start", (_event, ctx) => {
		record("session_start", {
			agentId: ctx.multiAgentAgentId ?? "main",
			sessionId: ctx.sessionManager.getSessionId(),
			tools: pi.getActiveTools(),
		});
	});
	pi.on("tool_result", (event, ctx) => {
		record("tool_result", {
			agentId: ctx.multiAgentAgentId ?? "main",
			toolName: event.toolName,
			isError: event.isError,
			content: event.content,
		});
		if (event.toolName === "send_agent_message") {
			record("mailbox_enqueued", {
				messages: ctx.multiAgentStore?.listMailboxMessages(),
			});
		}
	});
	pi.on("before_agent_start", (event, ctx) => {
		record("agent_start", {
			agentId: ctx.multiAgentAgentId ?? "main",
			prompt: event.prompt,
		});
	});
	pi.on("agent_end", (event, ctx) => {
		record("agent_end", {
			agentId: ctx.multiAgentAgentId ?? "main",
			messages: event.messages,
		});
	});
	pi.on("runtime_mailbox", (event, ctx) => {
		record("mailbox", {
			agentId: ctx.multiAgentAgentId ?? "main",
			message: event.message,
		});
	});
}
