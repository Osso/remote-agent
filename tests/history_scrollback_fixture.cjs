const fs = require("node:fs");
const { Terminal } = require("@xterm/headless");

function indexHistoryRows(lines) {
	const firstRows = new Map();
	for (const [row, line] of lines.entries()) {
		for (const match of line.matchAll(/HISTORY-\d{4}/g)) {
			if (!firstRows.has(match[0])) firstRows.set(match[0], row);
		}
	}
	return firstRows;
}

function inspectScrollback(terminal) {
	const buffer = terminal.buffer.normal;
	const lines = Array.from({ length: buffer.length }, (_, i) =>
		buffer.getLine(i).translateToString(true),
	);
	const firstRows = indexHistoryRows(lines);
	const positions = Array.from(
		{ length: 200 },
		(_, i) => firstRows.get(`HISTORY-${String(i + 1).padStart(4, "0")}`) ?? -1,
	);
	return {
		activeBuffer: terminal.buffer.active.type,
		markerCount: positions.filter((position) => position >= 0).length,
		ordered: positions.every(
			(position, i) =>
				position >= 0 && (i === 0 || position > positions[i - 1]),
		),
		lastHistoryRow: positions.at(-1),
		liveViewportStart: buffer.baseY,
		liveViewPresent: lines
			.slice(buffer.baseY)
			.some((line) => line.includes("LIVE-VIEW")),
	};
}

async function verifyScrollback() {
	const request = JSON.parse(fs.readFileSync(0, "utf8"));
	const terminal = new Terminal({
		cols: request.columns,
		rows: request.rows,
		scrollback: 2000,
		allowProposedApi: true,
		windowOptions: { pushTitle: true, popTitle: true },
	});
	const titles = [];
	terminal.onTitleChange((title) => titles.push(title));
	await new Promise((resolve) =>
		terminal.write("\x1b]2;previous-title\x07" + request.output, resolve),
	);
	const result = { ...inspectScrollback(terminal), titles };
	terminal.dispose();
	console.log(JSON.stringify(result));
	const hasNativeHistory = result.activeBuffer === "normal" && result.ordered;
	const hasSeparateLiveView =
		result.liveViewPresent && result.lastHistoryRow < result.liveViewportStart;
	const isComplete = hasNativeHistory && hasSeparateLiveView;
	if (!isComplete) process.exitCode = 1;
}

verifyScrollback().catch((error) => {
	console.error(error);
	process.exitCode = 1;
});
