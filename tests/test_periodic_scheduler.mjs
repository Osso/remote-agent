import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import {
	copyFileSync,
	existsSync,
	mkdirSync,
	mkdtempSync,
	rmSync,
	symlinkSync,
	writeFileSync,
} from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { pathToFileURL } from "node:url";

const interval = 30 * 60 * 1000;
const checklistBody =
	"# Periodic checks\n\nReport current Git status and test failures. No edits.\n";
const checklistContent = configuredChecklist(1800);

function configuredChecklist(seconds) {
	return `---\ntype: Periodic Checks\ninterval_seconds: ${seconds}\n---\n${checklistBody}`;
}

function linkPiPackage(home) {
	const require = createRequire(import.meta.url);
	const packageName = "@earendil-works/pi-coding-agent";
	const root = require.resolve
		.paths(packageName)
		.map((path) => join(path, packageName))
		.find((path) => existsSync(join(path, "package.json")));
	assert.ok(root, "Install Pi's npm package or expose it through NODE_PATH");
	const scope = join(home, "node_modules/@earendil-works");
	mkdirSync(scope, { recursive: true });
	symlinkSync(root, join(scope, "pi-coding-agent"));
}

function git(cwd, ...args) {
	return execFileSync("git", args, { cwd, encoding: "utf8", timeout: 5000 });
}

function createProject(home, name) {
	const root = join(home, name);
	mkdirSync(root, { recursive: true });
	git(root, "init", "--quiet");
	writeFileSync(join(root, "periodic-checks.md"), checklistContent);
	return root;
}

async function fixture(
	t,
	overrides = {},
	tools = ["spawn_agent", "send_agent_message"],
) {
	const home = mkdtempSync(join(tmpdir(), "periodic-test-"));
	t.after(() => rmSync(home, { recursive: true, force: true }));
	linkPiPackage(home);
	const project = createProject(home, "first-project");
	const secondProject = createProject(home, "second-project");
	const checklist = join(project, "periodic-checks.md");
	const extensionPath = join(home, "periodic-checks.ts");
	copyFileSync(
		new URL("../periodic-checks.ts", import.meta.url),
		extensionPath,
	);
	const globalDirectory = join(home, ".config/pi/agent/extensions");
	mkdirSync(globalDirectory, { recursive: true });
	const globalExtension = join(globalDirectory, "periodic-checks.ts");
	symlinkSync(extensionPath, globalExtension);
	const extensionUrl = pathToFileURL(globalExtension);
	t.mock.timers.enable({ apis: ["setInterval", "Date"], now: 1800000000000 });
	const handlers = new Map();
	const commands = new Map();
	const notifications = [];
	const messages = [];
	const agents = [];
	let idle = true;
	let pending = false;
	let sessionId = "main-session";
	const ctx = {
		cwd: project,
		ui: { notify: (text, type) => notifications.push({ text, type }) },
		mode: "tui",
		multiAgentAgentId: undefined,
		multiAgentRequiresAgentId: false,
		multiAgentStore: { listAgents: () => agents },
		sessionManager: { getSessionId: () => sessionId },
		isIdle: () => idle,
		hasPendingMessages: () => pending,
		...overrides,
	};
	const pi = {
		on: (name, handler) => handlers.set(name, handler),
		registerCommand: (name, command) => commands.set(name, command),
		getActiveTools: () => tools,
		sendMessage: (message, options) => messages.push({ message, options }),
	};
	const extension = (await import(extensionUrl.href)).default;
	extension(pi);
	const emit = (name, event = {}) => handlers.get(name)?.(event, ctx);
	const finishPrompt = (sessionContinuation) =>
		emit("agent_end", {
			messages: [{ role: "custom", ...messages.at(-1).message }],
			sessionContinuation,
		});
	return {
		messages,
		notifications,
		status: async (args = "status") => {
			const command = commands.get("periodic-checks");
			assert.ok(command, "periodic-checks command must be registered");
			await command.handler(args, ctx);
			return notifications.at(-1).text;
		},
		agents,
		emit,
		finishPrompt,
		ctx,
		home,
		project,
		secondProject,
		checklist,
		removeChecklist: () => rmSync(checklist),
		restoreChecklist: (content = checklistContent) =>
			writeFileSync(checklist, content),
		tick: (ms = interval) => t.mock.timers.tick(ms),
		busy: (value) => {
			idle = !value;
		},
		pending: (value) => {
			pending = value;
		},
		switchIdentity: (nextSessionId = "different-session") => {
			sessionId = nextSessionId;
		},
	};
}

test("status reports armed schedule without changing metadata or original deadline", async (t) => {
	const f = await fixture(t);
	f.restoreChecklist(configuredChecklist(60));
	f.emit("session_start");
	f.tick(30000);
	f.restoreChecklist(checklistBody);
	for (const args of ["status", "", "  status  "]) {
		assert.equal(
			await f.status(args),
			`Periodic checks: armed\nChecklist: ${f.checklist}\ninterval_seconds: 60\nNext run: in less than a minute`,
		);
	}
	assert.equal(f.messages.length, 0);
	f.tick(29999);
	assert.equal(f.messages.length, 0);
	f.tick(1);
	assert.equal(f.messages.length, 1);
});

test("status counts down without auditing, rereading metadata or resetting the original tick", async (t) => {
	const f = await fixture(t);
	f.emit("session_start");
	f.restoreChecklist(checklistBody);
	for (const [elapsed, expected] of [
		[0, "Next run: in 30 minutes"],
		[60000, "Next run: in 29 minutes"],
		[28 * 60000, "Next run: in 1 minute"],
		[1, "Next run: in less than a minute"],
	]) {
		f.tick(elapsed);
		for (let repeat = 0; repeat < 2; repeat++) {
			assert.equal(
				await f.status(),
				`Periodic checks: armed\nChecklist: ${f.checklist}\ninterval_seconds: 1800\n${expected}`,
			);
			assert.equal(f.notifications.at(-1).type, "info");
		}
		assert.equal(f.messages.length, 0);
	}
	f.tick(59998);
	assert.equal(f.messages.length, 0);
	f.tick(1);
	assert.equal(f.messages.length, 1);
});

test("status rounds 90 seconds up to 2 minutes", async (t) => {
	const f = await fixture(t);
	f.restoreChecklist(configuredChecklist(90));
	f.emit("session_start");
	assert.equal(
		await f.status(),
		`Periodic checks: armed\nChecklist: ${f.checklist}\ninterval_seconds: 90\nNext run: in 2 minutes`,
	);
});

test("status clamps due and overdue ticks to less than a minute without dispatch", async (t) => {
	const f = await fixture(t);
	f.emit("session_start");
	const start = Date.now();
	for (const elapsed of [interval, interval + 1, interval + 60000]) {
		t.mock.timers.setTime(start + elapsed);
		assert.equal(
			await f.status(),
			`Periodic checks: armed\nChecklist: ${f.checklist}\ninterval_seconds: 1800\nNext run: in less than a minute`,
		);
		assert.equal(f.messages.length, 0);
	}
});

test("status rejects controls and extra arguments without resetting deadline", async (t) => {
	const f = await fixture(t);
	f.emit("session_start");
	f.tick(interval - 1);
	for (const args of ["start", "stop", "status extra"]) {
		assert.equal(await f.status(args), "Usage: /periodic-checks status");
		assert.equal(f.notifications.at(-1).type, "warning");
	}
	assert.equal(f.messages.length, 0);
	f.tick(1);
	assert.equal(f.messages.length, 1);
});

test("status records missing checklist without rearming on restoration", async (t) => {
	const f = await fixture(t);
	f.removeChecklist();
	f.emit("session_start");
	assert.equal(
		await f.status(),
		`Periodic checks: disabled\nChecklist: ${f.checklist}\nReason: No checklist`,
	);
	f.restoreChecklist();
	await f.status();
	f.tick(interval * 2);
	assert.equal(f.messages.length, 0);
});

test("status preserves explicit invalid metadata startup error and disabled state", async (t) => {
	const f = await fixture(t);
	f.emit("session_start");
	f.restoreChecklist(checklistBody);
	let startupError;
	assert.throws(
		() => f.emit("session_start"),
		(error) => {
			startupError = error.message;
			return error.message.includes(f.checklist);
		},
	);
	assert.equal(
		await f.status(),
		`Periodic checks: disabled\nChecklist: ${f.checklist}\nReason: ${startupError}`,
	);
	f.restoreChecklist();
	await f.status();
	f.tick(interval * 2);
	assert.equal(f.messages.length, 0);
});

test("status restart resets interval, checklist and scheduled boundary", async (t) => {
	const f = await fixture(t);
	f.emit("session_start");
	f.tick(30000);
	f.emit("session_shutdown");
	assert.match(await f.status(), /disabled[\s\S]*Reason: Session shutdown/);
	assert.ok((await f.status()).includes(f.checklist));
	f.ctx.cwd = f.secondProject;
	writeFileSync(
		join(f.secondProject, "periodic-checks.md"),
		configuredChecklist(60),
	);
	f.emit("session_start", { reason: "restart" });
	assert.equal(
		await f.status(),
		`Periodic checks: armed\nChecklist: ${join(f.secondProject, "periodic-checks.md")}\ninterval_seconds: 60\nNext run: in 1 minute`,
	);
	f.tick(59999);
	assert.equal(f.messages.length, 0);
	f.tick(1);
	assert.equal(f.messages.length, 1);
});

test("status advances scheduled boundary through busy and in-flight skipped ticks", async (t) => {
	const f = await fixture(t);
	f.emit("session_start");
	f.busy(true);
	f.tick();
	assert.equal(
		await f.status(),
		`Periodic checks: armed\nChecklist: ${f.checklist}\ninterval_seconds: 1800\nNext run: in 30 minutes`,
	);
	f.tick(60000);
	assert.equal(
		await f.status(),
		`Periodic checks: armed\nChecklist: ${f.checklist}\ninterval_seconds: 1800\nNext run: in 29 minutes`,
	);
	assert.equal(f.messages.length, 0);
	f.busy(false);
	f.tick(interval - 60000);
	assert.equal(f.messages.length, 1);
	f.tick();
	assert.equal(
		await f.status(),
		`Periodic checks: armed\nChecklist: ${f.checklist}\ninterval_seconds: 1800\nNext run: in 30 minutes`,
	);
	assert.equal(f.messages.length, 1);
	f.finishPrompt();
	await f.status();
	assert.equal(f.messages.length, 1);
});

test("status reflects removal only after scheduled tick and never restarts timer", async (t) => {
	const f = await fixture(t);
	f.emit("session_start");
	f.removeChecklist();
	assert.match(await f.status(), /Periodic checks: armed/);
	f.busy(true);
	f.tick();
	assert.equal(
		await f.status(),
		`Periodic checks: disabled\nChecklist: ${f.checklist}\nReason: Checklist removed`,
	);
	f.restoreChecklist();
	f.busy(false);
	await f.status();
	f.tick(interval * 2);
	assert.equal(f.messages.length, 0);
});

test("status explains unsupported runtimes and missing tools", async (t) => {
	for (const [overrides, tools, reason] of [
		[{ multiAgentAgentId: "child-1" }, undefined, "Child session"],
		[{ multiAgentRequiresAgentId: true }, undefined, "Child session"],
		[{ mode: "print" }, undefined, "Unsupported runtime mode: print"],
		[
			{ multiAgentStore: undefined },
			undefined,
			"Missing multi-agent runtime store",
		],
		[{}, ["send_agent_message"], "Missing required tools: spawn_agent"],
		[{}, ["spawn_agent"], "Missing required tools: send_agent_message"],
	]) {
		await t.test(reason, async (t) => {
			const f = await fixture(t, overrides, tools);
			f.emit("session_start");
			assert.equal(
				await f.status(),
				`Periodic checks: disabled\nReason: ${reason}`,
			);
			f.tick(interval);
			assert.equal(f.messages.length, 0);
		});
	}
});

test("status explains non-Git cwd without stale checklist or interval", async (t) => {
	const f = await fixture(t);
	f.emit("session_start");
	f.ctx.cwd = f.home;
	f.emit("session_start");
	assert.equal(
		await f.status(),
		"Periodic checks: disabled\nReason: Not a Git project",
	);
	f.tick(interval);
	assert.equal(f.messages.length, 0);
});

test("1800-second metadata delays first and recurring bounded report-only audit", async (t) => {
	const f = await fixture(t);
	f.tick();
	assert.equal(f.messages.length, 0, "factory must not start a timer");
	f.emit("session_start");
	f.emit("agent_end", { messages: [] });
	f.tick(interval - 1);
	assert.equal(f.messages.length, 0);
	f.tick(1);
	assert.equal(f.messages.length, 1);
	const { message, options } = f.messages[0];
	assert.deepEqual(options, { deliverAs: "followUp", triggerTurn: true });
	for (const text of [
		"spawn_agent",
		"read-only",
		"bounded",
		"send_agent_message",
		"worktree",
		"disk",
		"build",
		"error",
		"delegation",
		"headroom",
		"no cleanup",
		"no blocking",
		"no cancellation",
		"no steering",
		"periodic-checks.md",
		"exactly one",
		"no edits or deployment",
		"do not audit in main",
		"delegate nested children",
	]) {
		assert.ok(
			message.content.includes(text),
			`missing audit instruction: ${text}`,
		);
	}
	assert.ok(message.content.includes(f.checklist));
	f.finishPrompt();
	f.tick(interval - 1);
	assert.equal(f.messages.length, 1);
	f.tick(1);
	assert.equal(
		f.messages.length,
		2,
		"recurring audit exactly 30 minutes later",
	);
});

for (const seconds of [60, 1, 2147483]) {
	test(`${seconds}-second metadata delays first and recurring audit`, async (t) => {
		const f = await fixture(t);
		f.restoreChecklist(configuredChecklist(seconds));
		f.emit("session_start");
		const delay = seconds * 1000;
		f.tick(delay - 1);
		assert.equal(f.messages.length, 0);
		f.tick(1);
		assert.equal(f.messages.length, 1);
		f.finishPrompt();
		f.tick(delay - 1);
		assert.equal(f.messages.length, 1);
		f.tick(1);
		assert.equal(f.messages.length, 2);
	});
}

const invalidChecklists = [
	["missing frontmatter", checklistBody],
	[
		"unclosed frontmatter",
		"---\ntype: Periodic Checks\ninterval_seconds: 60\n",
	],
	[
		"bad opening delimiter",
		"---oops\ntype: Periodic Checks\ninterval_seconds: 60\n---\n",
	],
	[
		"bad closing delimiter",
		"---\ntype: Periodic Checks\ninterval_seconds: 60\n---oops\n",
	],
	["empty metadata", "---\n---\n"],
	["sequence metadata", "---\n- Periodic Checks\n- 60\n---\n"],
	["scalar metadata", "---\nPeriodic Checks\n---\n"],
	["missing type", "---\ninterval_seconds: 60\n---\n"],
	["missing interval", "---\ntype: Periodic Checks\n---\n"],
	[
		"duplicate type",
		"---\ntype: First\ntype: Second\ninterval_seconds: 60\n---\n",
	],
	[
		"duplicate interval",
		"---\ntype: Periodic Checks\ninterval_seconds: 60\ninterval_seconds: 1800\n---\n",
	],
	[
		"malformed YAML",
		"---\ntype: Periodic Checks\ninterval_seconds: [60\n---\n",
	],
	[
		"malformed indentation",
		"---\ntype: Periodic Checks\ninterval_seconds:\n\tvalue: 60\n---\n",
	],
	...["''", "'  '", "null", "123", "[]", "{}"].map((value) => [
		`invalid type ${value}`,
		`---\ntype: ${value}\ninterval_seconds: 60\n---\n`,
	]),
	...[
		"0",
		"-1",
		"0.5",
		"1.5",
		".nan",
		".inf",
		"-.inf",
		"9007199254740992",
		"2147484",
		"'60'",
		"true",
		"null",
		"[]",
		"{}",
	].map((value) => [`invalid interval ${value}`, configuredChecklist(value)]),
];

for (const [label, content] of invalidChecklists) {
	test(`${label} reports checklist path and never schedules`, async (t) => {
		const f = await fixture(t);
		f.restoreChecklist(content);
		assert.throws(
			() => f.emit("session_start"),
			(error) => {
				assert.ok(error instanceof Error);
				assert.ok(error.message.includes(f.checklist));
				assert.match(error.message, /metadata/i);
				return true;
			},
		);
		f.tick(interval * 3);
		assert.equal(f.messages.length, 0);
	});
}

test("standard YAML unknown nested keys, aliases, flow mappings, and block strings are accepted", async (t) => {
	const f = await fixture(t);
	f.restoreChecklist(
		`---\ntype: >-\n  Custom Checks\ninterval_seconds: &delay 60\nfuture: {copied_delay: *delay, tags: [one, two]}\nnotes: |\n  Unknown multiline metadata\n  is allowed.\n---\n${checklistBody}`,
	);
	f.emit("session_start");
	f.tick(59999);
	assert.equal(f.messages.length, 0);
	f.tick(1);
	assert.equal(f.messages.length, 1);
});

test("CRLF frontmatter supports quoted YAML keys and values", async (t) => {
	const f = await fixture(t);
	f.restoreChecklist(
		'---\r\n"type": \'Periodic Checks\'\r\n"interval_seconds": 60 # seconds\r\n---\r\nBody\r\n',
	);
	f.emit("session_start");
	f.tick(60000);
	assert.equal(f.messages.length, 1);
});

test("reload and restart reread metadata and wait the full new delay", async (t) => {
	const f = await fixture(t);
	f.restoreChecklist(configuredChecklist(60));
	f.emit("session_start");
	f.tick(59000);
	f.restoreChecklist(configuredChecklist(1800));
	f.emit("session_start", { reason: "reload" });
	f.tick(interval - 1);
	assert.equal(f.messages.length, 0);
	f.tick(1);
	assert.equal(f.messages.length, 1);
	f.emit("session_shutdown");
	f.restoreChecklist(configuredChecklist(60));
	f.emit("session_start", { reason: "restart" });
	f.tick(59999);
	assert.equal(f.messages.length, 1);
	f.tick(1);
	assert.equal(f.messages.length, 2);
});

test("metadata edits do not change an armed delay until session_start", async (t) => {
	const f = await fixture(t);
	f.restoreChecklist(configuredChecklist(60));
	f.emit("session_start");
	f.restoreChecklist(configuredChecklist(1800));
	f.tick(60000);
	assert.equal(f.messages.length, 1);
});

test("invalid metadata on reload clears the previously armed timer", async (t) => {
	const f = await fixture(t);
	f.emit("session_start");
	f.tick(interval - 1);
	f.restoreChecklist(checklistBody);
	assert.throws(() => f.emit("session_start", { reason: "reload" }));
	f.tick(interval * 3);
	assert.equal(f.messages.length, 0);
});

test("audit prompt routes child findings to originating main session and confirms mailbox receipt", async (t) => {
	const f = await fixture(t);
	f.emit("session_start");
	f.tick();
	assert.equal(f.messages.length, 1);
	const prompt = f.messages[0].message.content;
	assert.ok(
		prompt.includes(
			'send_agent_message with toAgentId "main" AND toSessionId "main-session"',
		),
	);
	assert.ok(
		prompt.includes("Check the tool result and confirm successful delivery"),
	);
	assert.ok(prompt.includes("Main must confirm mailbox receipt"));
	assert.ok(prompt.includes("not just wait_agent recovery"));
});

test("replacement and restarted sessions route audits to current parent identity", async (t) => {
	const f = await fixture(t);
	f.emit("session_start");
	f.tick();
	assert.equal(f.messages.length, 1);
	assert.ok(
		f.messages[0].message.content.includes('toSessionId "main-session"'),
	);
	for (const [reason, sessionId] of [
		["replacement", "replacement-main-session"],
		["restart", "restarted-main-session"],
	]) {
		if (reason === "restart") f.emit("session_shutdown");
		f.switchIdentity(sessionId);
		f.emit("session_start", { reason });
		f.tick();
		const prompt = f.messages.at(-1).message.content;
		assert.ok(prompt.includes(`toSessionId "${sessionId}"`));
		assert.ok(!prompt.includes('toSessionId "main-session"'));
	}
	assert.equal(f.messages.length, 3);
});

test("second Git project opts in without remote-agent mapping", async (t) => {
	const f = await fixture(t);
	f.ctx.cwd = f.secondProject;
	f.emit("session_start");
	f.tick();
	assert.equal(f.messages.length, 1);
	assert.ok(
		f.messages[0].message.content.includes(
			join(f.secondProject, "periodic-checks.md"),
		),
	);
	assert.ok(!f.messages[0].message.content.includes(f.checklist));
});

test("nested cwd discovers checklist at Git project root", async (t) => {
	const f = await fixture(t);
	f.ctx.cwd = join(f.project, "src/nested");
	mkdirSync(f.ctx.cwd, { recursive: true });
	writeFileSync(
		join(f.ctx.cwd, "periodic-checks.md"),
		"Not the project checklist",
	);
	f.emit("session_start");
	f.tick();
	assert.equal(f.messages.length, 1);
	assert.ok(f.messages[0].message.content.includes(f.checklist));
	assert.ok(
		!f.messages[0].message.content.includes(
			join(f.ctx.cwd, "periodic-checks.md"),
		),
	);
});

test("Git worktree uses its own checklist, not canonical checkout", async (t) => {
	const f = await fixture(t);
	git(f.project, "add", "periodic-checks.md");
	git(
		f.project,
		"-c",
		"user.name=Periodic Test",
		"-c",
		"user.email=periodic@example.test",
		"-c",
		"commit.gpgsign=false",
		"-c",
		"core.hooksPath=/dev/null",
		"commit",
		"--quiet",
		"-m",
		"Add checklist",
	);
	const worktree = join(f.home, "linked-worktree");
	git(f.project, "worktree", "add", "--quiet", "--detach", worktree);
	f.removeChecklist();
	f.ctx.cwd = join(worktree, "src");
	mkdirSync(f.ctx.cwd);
	f.emit("session_start");
	f.tick();
	assert.equal(f.messages.length, 1);
	assert.ok(
		f.messages[0].message.content.includes(
			join(worktree, "periodic-checks.md"),
		),
	);
	assert.ok(!f.messages[0].message.content.includes(f.checklist));
});

test("busy and queued input ticks are skipped, never dispatched on completion", async (t) => {
	const f = await fixture(t);
	f.emit("session_start");
	f.busy(true);
	f.tick();
	f.busy(false);
	f.emit("agent_end", { messages: [] });
	assert.equal(f.messages.length, 0);
	f.pending(true);
	f.tick();
	f.pending(false);
	f.emit("agent_end", { messages: [] });
	assert.equal(f.messages.length, 0);
	f.tick();
	assert.equal(f.messages.length, 1);
});

test("queued audit and live audit child suppress overlap until child terminal state", async (t) => {
	const f = await fixture(t);
	f.emit("session_start");
	f.tick();
	f.tick();
	f.emit("agent_end", { messages: [] });
	f.tick();
	assert.equal(f.messages.length, 1);
	f.finishPrompt("cwd_relocation");
	f.tick();
	assert.equal(f.messages.length, 1);
	f.agents.push({
		id: "audit-child",
		displayName: "periodic-checks-audit",
		lifecycle: "running",
	});
	f.finishPrompt();
	f.tick();
	assert.equal(f.messages.length, 1);
	f.agents[0].lifecycle = "waiting_for_input";
	f.tick();
	assert.equal(f.messages.length, 1);
	f.agents[0].lifecycle = "completed";
	f.emit("agent_end", { messages: [] });
	assert.equal(f.messages.length, 1, "no completion-triggered audit");
	f.tick();
	assert.equal(f.messages.length, 2);
});

test("every session_start rearms from zero; shutdown clears; stale identity cannot fire", async (t) => {
	const f = await fixture(t);
	f.emit("session_start");
	f.tick(interval - 1);
	f.emit("session_start", { reason: "reload" });
	f.tick(interval - 1);
	assert.equal(f.messages.length, 0);
	f.tick(1);
	assert.equal(f.messages.length, 1);
	f.emit("session_shutdown");
	f.tick(interval * 3);
	assert.equal(f.messages.length, 1);
	f.emit("session_start", { reason: "resume" });
	f.tick(interval - 1);
	assert.equal(f.messages.length, 1);
	f.tick(1);
	assert.equal(f.messages.length, 2);
	f.finishPrompt();
	f.switchIdentity();
	f.tick();
	assert.equal(f.messages.length, 2);
});

test("only supported Pi main runtimes arm", async (t) => {
	for (const [label, overrides, tools] of [
		["child", { multiAgentAgentId: "child-1" }],
		["child awaiting identity", { multiAgentRequiresAgentId: true }],
		["print", { mode: "print" }],
		["json", { mode: "json" }],
		["missing runtime store", { multiAgentStore: undefined }],
		["missing delegation", {}, ["send_agent_message"]],
		["missing mailbox", {}, ["spawn_agent"]],
	]) {
		await t.test(label, async (t) => {
			const f = await fixture(t, overrides, tools);
			f.emit("session_start");
			f.tick(interval * 2);
			assert.equal(f.messages.length, 0);
		});
	}
});

test("absent checklist disables scheduling until restored and session_start fires", async (t) => {
	const f = await fixture(t);
	f.removeChecklist();
	f.emit("session_start");
	f.tick();
	assert.equal(f.messages.length, 0, "absent checklist must not arm a timer");
	f.restoreChecklist();
	f.tick(interval * 2);
	assert.equal(
		f.messages.length,
		0,
		"restoration must not start polling audits",
	);
	f.emit("session_start");
	f.tick(interval - 1);
	assert.equal(f.messages.length, 0);
	f.tick(1);
	assert.equal(f.messages.length, 1);
});

test("removed checklist prevents the next armed audit", async (t) => {
	const f = await fixture(t);
	f.emit("session_start");
	f.removeChecklist();
	f.tick();
	assert.equal(f.messages.length, 0, "removed checklist must not audit");
});

test("removed checklist stops armed timer even while busy; restoration requires session_start", async (t) => {
	const f = await fixture(t);
	f.emit("session_start");
	f.busy(true);
	f.removeChecklist();
	f.tick();
	assert.equal(f.messages.length, 0, "removed checklist must not audit");
	f.restoreChecklist();
	f.busy(false);
	f.tick(interval * 2);
	assert.equal(f.messages.length, 0, "removed checklist must stop the timer");
	f.emit("session_start");
	f.tick(interval - 1);
	assert.equal(f.messages.length, 0);
	f.tick(1);
	assert.equal(f.messages.length, 1);
});

test("ticks remain scoped to startup project after cwd changes", async (t) => {
	const f = await fixture(t);
	f.emit("session_start");
	f.ctx.cwd = f.secondProject;
	f.tick();
	assert.equal(f.messages.length, 0);
	f.emit("session_start");
	f.tick();
	assert.equal(f.messages.length, 1);
	assert.ok(
		f.messages[0].message.content.includes(
			join(f.secondProject, "periodic-checks.md"),
		),
	);
});

test("non-Git directory with checklist cannot opt in", async (t) => {
	const f = await fixture(t);
	f.ctx.cwd = f.home;
	writeFileSync(join(f.home, "periodic-checks.md"), checklistContent);
	f.emit("session_start");
	f.tick(interval * 2);
	assert.equal(f.messages.length, 0);
});

test("stale generation completion cannot clear current queued audit", async (t) => {
	const f = await fixture(t);
	f.emit("session_start");
	f.tick();
	assert.equal(f.messages.length, 1);
	const stale = { role: "custom", ...f.messages[0].message };
	f.emit("session_start");
	f.tick();
	f.emit("agent_end", { messages: [stale] });
	f.tick();
	assert.equal(f.messages.length, 2);
	f.finishPrompt();
	f.tick();
	assert.equal(f.messages.length, 3);
});
