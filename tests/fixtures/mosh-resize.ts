import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);
const [mode, argument] = process.argv.slice(2);
const paragraphCount = 80;
const sentinels = ["REPORT_END", "END_TURN_OLD", "SUPERVISOR_NEW", "LATER_REPLY_NEW", "END_TURN_NEW"];

function canonicalHistory(): string {
	const narrative =
		"unique prose about rendering carries enough words to wrap differently at narrow and wide terminal sizes.";
	const paragraphs = Array.from(
		{ length: paragraphCount },
		(_, index) => `PROOF_${String(index).padStart(3, "0")} ${narrative}`,
	);
	return [
		...paragraphs,
		"| TABLE_KEY | RESULT | EVIDENCE |",
		"| table-one | complete | bounded diagnostic |",
		...sentinels,
	].join("\n");
}

async function replayTerminal(): Promise<void> {
	const { Terminal } = require("@xterm/headless");
	const terminal = new Terminal({
		cols: 70,
		rows: 16,
		scrollback: 5000,
		allowProposedApi: true,
	});
	const write = (value: string) => new Promise<void>((resolve) => terminal.write(value, resolve));
	const stages = JSON.parse(fs.readFileSync(0, "utf8"));
	await write("STALE_BEFORE_ATTACH\r\n".repeat(30));
	const reports = [];
	for (const stage of stages) {
		terminal.resize(stage.columns, stage.rows);
		await write(stage.output);
		const buffer = terminal.buffer.active;
		const lines = Array.from({ length: buffer.length }, (_, index) => buffer.getLine(index).translateToString(true));
		reports.push({
			name: stage.name,
			history: lines.slice(0, buffer.baseY),
			visible: lines.slice(buffer.baseY, buffer.baseY + stage.rows),
			clearScrollback: stage.output.includes("\x1b[3J"),
		});
	}
	terminal.dispose();
	process.stdout.write(JSON.stringify({ reports, sentinels, paragraphCount }));
}

async function replayLiveTerminal(): Promise<void> {
	const { Terminal } = require("@xterm/headless");
	const terminal = new Terminal({ cols: 165, rows: 46, scrollback: 5000, allowProposedApi: true });
	const stages = JSON.parse(fs.readFileSync(0, "utf8"));
	const reports = [];
	for (const stage of stages) {
		terminal.resize(stage.columns ?? 165, stage.rows ?? 46);
		await new Promise<void>((resolve) => terminal.write(stage.output, resolve));
		const buffer = terminal.buffer.active;
		const lines = Array.from({ length: buffer.length }, (_, index) => buffer.getLine(index).translateToString(true));
		reports.push({
			name: stage.name,
			history: lines.slice(0, buffer.baseY),
			visible: lines.slice(buffer.baseY, buffer.baseY + terminal.rows),
		});
	}
	terminal.dispose();
	process.stdout.write(JSON.stringify(reports));
}

function bulkAssistantHistory(revised: boolean): string {
	const lines = Array.from(
		{ length: 80 },
		(_, index) => `BULK_LINE_${String(index).padStart(3, "0")} assistant paragraph`,
	);
	const table = revised
		? `| status | evidence |\n| --- | --- |\n| TABLE_EXPANDED | ${"wide table cell with wrapping prose ".repeat(12)} |`
		: "| status | evidence |\n| TABLE_PENDING | incomplete streamed table |";
	const heading = revised ? "# Revised Markdown heading" : "**Unfinished Markdown heading";
	return [heading, lines.slice(0, 5).join("  \n"), table, lines.slice(5).join("  \n")].join("\n\n");
}

async function renderBulkAssistant(source: string): Promise<void> {
	const load = (relative: string) => import(pathToFileURL(path.join(source, relative)).href);
	const components = "../../coding-agent/src/modes/interactive/components/";
	const { ProcessTerminal } = await load("terminal.ts");
	const { TUI } = await load("tui.ts");
	const { AssistantMessageComponent } = await load(`${components}assistant-message.ts`);
	const { CustomMessageComponent } = await load(`${components}custom-message.ts`);
	const { ToolExecutionComponent } = await load(`${components}tool-execution.ts`);
	const { initTheme } = await load("../../coding-agent/src/modes/interactive/theme/theme.ts");
	initTheme("dark", false);
	const tui = new TUI(new ProcessTerminal(), true);
	const assistant = new AssistantMessageComponent();
	const tool = new ToolExecutionComponent("diagnostic", "fixture-call", {}, {}, undefined, tui, process.cwd());
	tui.addChild(assistant);
	const updateAssistant = (revised: boolean) => {
		assistant.updateContent({
			role: "assistant",
			content: [{ type: "text", text: bulkAssistantHistory(revised) }],
			stopReason: "stop",
		});
	};
	const recordPhase = (phase: string) => {
		tui.requestRender();
		fs.appendFileSync(argument, JSON.stringify({ phase, pid: process.pid }) + "\n");
	};
	tui.addInputListener((input: string) => {
		if (input.includes("b")) {
			updateAssistant(false);
			recordPhase("bulk");
		}
		if (input.includes("r")) {
			updateAssistant(true);
			recordPhase("revised");
		}
		if (input.includes("w")) {
			tool.updateResult({ content: [{ type: "text", text: "TOOL_WIDGET pending" }], isError: false }, true);
			tui.addChild(tool);
			tui.addChild(
				new CustomMessageComponent({
					role: "custom",
					customType: "fixture-event",
					content: "EVENT_WIDGET later event",
					display: true,
					timestamp: 0,
				}),
			);
			recordPhase("widgets");
		}
		if (input.includes("p")) {
			tool.updateResult({ content: [{ type: "text", text: "TOOL_WIDGET complete" }], isError: false });
			recordPhase("partial");
		}
		return { consume: true };
	});
	process.on("SIGTERM", () => {
		tui.stop();
		process.exit(0);
	});
	tui.start();
	recordPhase("ready");
	setInterval(() => {}, 1000);
}

async function renderLiveAssistant(source: string): Promise<void> {
	const load = (relative: string) => import(pathToFileURL(path.join(source, relative)).href);
	const { ProcessTerminal } = await load("terminal.ts");
	const { TUI } = await load("tui.ts");
	const { AssistantMessageComponent } = await load(
		"../../coding-agent/src/modes/interactive/components/assistant-message.ts",
	);
	const { initTheme } = await load("../../coding-agent/src/modes/interactive/theme/theme.ts");
	initTheme("dark", false);
	const tui = new TUI(new ProcessTerminal(), true);
	const assistant = new AssistantMessageComponent();
	tui.addChild(assistant);
	const lines: string[] = [];
	let streaming = false;
	const streamLines = async (count: number, phase: string) => {
		streaming = true;
		for (let index = 0; index < count; index++) {
			lines.push(`LIVE_LINE_${String(lines.length).padStart(3, "0")} assistant output`);
			assistant.updateContent({
				role: "assistant",
				content: [{ type: "text", text: lines.join("  \n") }],
				stopReason: "stop",
			});
			tui.requestRender();
			await new Promise((resolve) => setTimeout(resolve, 80));
		}
		fs.appendFileSync(argument, JSON.stringify({ phase, count: lines.length, pid: process.pid }) + "\n");
		streaming = false;
	};
	tui.addInputListener((input: string) => {
		if (!streaming && input.includes("s") && lines.length === 0) {
			void streamLines(80, "streamed");
		}
		if (!streaming && input.includes("d") && lines.length === 80) {
			void streamLines(16, "delayed");
		}
		return { consume: true };
	});
	process.on("SIGTERM", () => {
		tui.stop();
		process.exit(0);
	});
	tui.start();
	fs.appendFileSync(argument, JSON.stringify({ phase: "ready", pid: process.pid }) + "\n");
	setInterval(() => {}, 1000);
}

async function renderFixture(): Promise<void> {
	const source = process.env.PI_TUI_SOURCE;
	if (!source) throw new Error("PI_TUI_SOURCE must point to packages/tui/src");
	const load = (relative: string) => import(pathToFileURL(path.join(source, relative)).href);
	if (mode === "live-tui") return renderLiveAssistant(source);
	if (mode === "bulk-tui") return renderBulkAssistant(source);
	const { Text } = await load("components/text.ts");
	const text = new Text(canonicalHistory(), 0, 0);
	if (mode === "export") {
		process.stdout.write(text.render(Number(argument)).join("\n") + "\n");
		return;
	}
	if (mode !== "tui") throw new Error(`Unknown fixture mode: ${mode}`);
	const { ProcessTerminal } = await load("terminal.ts");
	const { TUI } = await load("tui.ts");
	const terminal = new ProcessTerminal();
	const tui = new TUI(terminal, true);
	tui.addChild(text);
	tui.addInputListener((input: string) => {
		if (input.includes("n")) {
			text.setText(`${canonicalHistory()}\nLIVE_NEW_REPLY`);
			fs.appendFileSync(argument, JSON.stringify({ pid: process.pid }) + "\n");
			tui.requestRender();
		}
		return { consume: true };
	});
	process.on("SIGTERM", () => {
		tui.stop();
		process.exit(0);
	});
	tui.start();
	setInterval(() => {}, 1000);
}

if (mode === "analyze") await replayTerminal();
else if (mode === "analyze-live") await replayLiveTerminal();
else await renderFixture();
