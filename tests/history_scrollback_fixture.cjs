const fs = require("node:fs");
const { Terminal } = require("@xterm/headless");

function inspectScrollback(terminal) {
	const buffer = terminal.buffer.normal;
	const lines = Array.from({ length: buffer.length }, (_, i) =>
		buffer.getLine(i).translateToString(true),
	);
	const positions = Array.from({ length: 200 }, (_, i) =>
		lines.findIndex((line) =>
			line.includes(`HISTORY-${String(i + 1).padStart(4, "0")}`),
		),
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
	});
	await new Promise((resolve) => terminal.write(request.output, resolve));
	const result = inspectScrollback(terminal);
	terminal.dispose();
	console.log(JSON.stringify(result));
	const complete =
		result.activeBuffer === "normal" &&
		result.ordered &&
		result.liveViewPresent &&
		result.lastHistoryRow < result.liveViewportStart;
	if (!complete) process.exitCode = 1;
}

verifyScrollback().catch((error) => {
	console.error(error);
	process.exitCode = 1;
});
