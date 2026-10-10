import type {
	ExtensionAPI,
	ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

const intervalMs = 30 * 60 * 1000;
const messageType = "periodic-checks";
const auditName = "periodic-checks-audit";
const terminalStates = new Set(["completed", "failed", "aborted"]);
const gitTimeoutMs = 5000;
const gitMaxBuffer = 64 * 1024;

function queryProjectRoot(cwd: string): string | undefined {
	const git = spawnSync("git", ["rev-parse", "--show-toplevel"], {
		cwd,
		encoding: "utf8",
		timeout: gitTimeoutMs,
		maxBuffer: gitMaxBuffer,
	});
	if (git.error) {
		throw new Error(`Cannot discover Git project root at ${cwd}`, {
			cause: git.error,
		});
	}
	if (git.status !== 0) return undefined;
	return git.stdout.trim() || undefined;
}

function supportsAudit(pi: ExtensionAPI, ctx: ExtensionContext): boolean {
	if (ctx.multiAgentAgentId || ctx.multiAgentRequiresAgentId) return false;
	if (ctx.mode !== "tui" && ctx.mode !== "rpc") return false;
	if (!ctx.multiAgentStore) return false;
	const tools = pi.getActiveTools();
	return tools.includes("spawn_agent") && tools.includes("send_agent_message");
}

function hasLiveAudit(ctx: ExtensionContext): boolean {
	return Boolean(
		ctx.multiAgentStore?.listAgents().some((agent) => {
			return (
				agent.displayName === auditName && !terminalStates.has(agent.lifecycle)
			);
		}),
	);
}

function hasAuditOverlap(ctx: ExtensionContext): boolean {
	if (!ctx.isIdle() || ctx.hasPendingMessages()) return true;
	return hasLiveAudit(ctx);
}

function auditPrompt(checklist: string, parentSessionId: string): string {
	return [
		`Periodic project monitor: use spawn_agent now with agentType "explore", context "fresh", displayName "${auditName}".`,
		`Delegate exactly one read-only, bounded audit child; do not audit in main or delegate nested children. Child must reread the current project's checklist at ${JSON.stringify(checklist)} for this audit.`,
		"Examine this project's worktree/disk/build/error/delegation state and host headroom, following the checklist. Report missing checklist or unavailable evidence explicitly.",
		`Include this exact delivery instruction in the child prompt: Send child findings through send_agent_message with toAgentId "main" AND toSessionId ${JSON.stringify(parentSessionId)}, the originating parent session ID. Check the tool result and confirm successful delivery; a failed tool call is not delivery. Main must confirm mailbox receipt, not just wait_agent recovery. Main reports findings only. Monitor report only: no cleanup, no blocking, no cancellation, no steering, no edits or deployment. Continue authorized work independently.`,
	].join("\n");
}

type ScheduledAudit = {
	projectRoot: string;
	checklist: string;
	sessionId: string;
	generation: number;
};

export default function periodicExtension(pi: ExtensionAPI) {
	let timer: ReturnType<typeof setInterval> | undefined;
	let generation = 0;
	let inFlight = false;

	function stopTimer(): void {
		if (timer !== undefined) clearInterval(timer);
		timer = undefined;
		inFlight = false;
		generation++;
	}

	function dispatchAuditTick(
		ctx: ExtensionContext,
		audit: ScheduledAudit,
	): void {
		if (!existsSync(audit.checklist)) {
			stopTimer();
			return;
		}
		if (audit.generation !== generation || inFlight) return;
		if (ctx.sessionManager.getSessionId() !== audit.sessionId) return;
		if (!supportsAudit(pi, ctx)) return;
		if (queryProjectRoot(ctx.cwd) !== audit.projectRoot) return;
		if (hasAuditOverlap(ctx)) return;
		pi.sendMessage(
			{
				customType: messageType,
				content: auditPrompt(audit.checklist, audit.sessionId),
				display: true,
				details: { generation: audit.generation, sessionId: audit.sessionId },
			},
			{ deliverAs: "followUp", triggerTurn: true },
		);
		inFlight = true;
	}

	function startTimer(ctx: ExtensionContext): void {
		stopTimer();
		if (!supportsAudit(pi, ctx)) return;
		const projectRoot = queryProjectRoot(ctx.cwd);
		if (!projectRoot) return;
		const checklist = join(projectRoot, "periodic-checks.md");
		if (!existsSync(checklist)) return;
		const audit = {
			projectRoot,
			checklist,
			sessionId: ctx.sessionManager.getSessionId(),
			generation,
		};
		timer = setInterval(() => dispatchAuditTick(ctx, audit), intervalMs);
	}

	pi.on("session_shutdown", stopTimer);
	pi.on("session_start", (_event, ctx) => startTimer(ctx));
	pi.on("agent_end", (event, ctx) => {
		if (event.sessionContinuation) return;
		const completedPrompt = event.messages.some((message) => {
			if (message.role !== "custom" || message.customType !== messageType)
				return false;
			const details = message.details as
				| { generation?: number; sessionId?: string }
				| undefined;
			return (
				details?.generation === generation &&
				details.sessionId === ctx.sessionManager.getSessionId()
			);
		});
		if (completedPrompt) inFlight = false;
	});
}
