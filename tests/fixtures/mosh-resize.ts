import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);
const [mode, argument] = process.argv.slice(2);
const paragraphCount = 80;
const sentinels = [
	"REPORT_END",
	"END_TURN_OLD",
	"SUPERVISOR_NEW",
	"LATER_REPLY_NEW",
	"END_TURN_NEW",
];

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
	const write = (value: string) =>
		new Promise<void>((resolve) => terminal.write(value, resolve));
	const stages = JSON.parse(fs.readFileSync(0, "utf8"));
	await write("STALE_BEFORE_ATTACH\r\n".repeat(30));
	const reports = [];
	for (const stage of stages) {
		terminal.resize(stage.columns, stage.rows);
		await write(stage.output);
		const buffer = terminal.buffer.active;
		const lines = Array.from({ length: buffer.length }, (_, index) =>
			buffer.getLine(index).translateToString(true),
		);
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

async function renderFixture(): Promise<void> {
	const source = process.env.PI_TUI_SOURCE;
	if (!source) throw new Error("PI_TUI_SOURCE must point to packages/tui/src");
	const load = (relative: string) =>
		import(pathToFileURL(path.join(source, relative)).href);
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
else await renderFixture();
