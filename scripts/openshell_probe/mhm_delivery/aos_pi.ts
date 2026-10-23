// aos_pi: SPIKE extension for the delivery probes on pi 1.1.0 and omp 18.8.3 (loaded by -e; no imports beyond node).
// It never talks to the bus itself: every bus interaction goes through the existing aos-agent hook entry points
// (`aos-agent inbox --hook`, `aos-agent stop --hook`), spawned as child processes, so the one tool implementation,
// ledger and scrubbing stay in aos-agent. Variants are selected by environment variables so one file measures them all:
//   AOS_PI_DELIVER   how an inbound message reaches the model after a tool call (handler: tool_result)
//                    content    append a text block to the tool result (works on both flavours per the Pi mechanisms probe)
//                    additional return { additionalContext } (omp documents it; pi 1.1.0's type lacks it)
//                    steer      pi.sendUserMessage(text, { deliverAs: "steer" })
//                    message    pi.sendMessage({customType:"aos-inbound", content, display:true}, { deliverAs: "steer" })
//                    aside      the same with deliverAs "aside" (omp only per its docs)
//                    none       pull nothing (negative control: the model must poll)
//   AOS_PI_STOP      what happens when the stop hook blocks: followup (sendUserMessage followUp) | boundary (return
//                    { entries: [custom_message], continue: true }) | none
//   AOS_PI_STOP_EVENT which event runs the stop hook: turn_end (only when the turn produced no tool results, i.e. the
//                    assistant's final answer) | agent_before_settle (pi) | agent_end
//   AOS_AGENT_BIN    the aos-agent script (default /usr/local/bin/aos-agent)
// Fire log: $HOME/.local/share/aos-pi-probe/fires.jsonl (HOME is /sandbox on pi, /tmp/pi-home on omp in the runner).
import { spawnSync } from "node:child_process";
import { appendFileSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

const AGENT = process.env.AOS_AGENT_BIN ?? "/usr/local/bin/aos-agent";
const DELIVER = process.env.AOS_PI_DELIVER ?? "content";
const STOP = process.env.AOS_PI_STOP ?? "followup";
const STOP_EVENT = process.env.AOS_PI_STOP_EVENT ?? "turn_end";
const LOG = join(homedir(), ".local", "share", "aos-pi-probe", "fires.jsonl");
// A2 measurement: one extension instance per process, or one per session? (instance id minted at module load)
const INSTANCE = Math.random().toString(36).slice(2, 10);
const LOADED_AT = Date.now() / 1000;

function fire(line: Record<string, unknown>): void {
	try {
		mkdirSync(dirname(LOG), { recursive: true });
		appendFileSync(LOG, `${JSON.stringify({ t: Date.now() / 1000, ...line })}\n`);
	} catch {
		// never fail the session on logging
	}
}

// The hook's largest legal output: 20 pending rows (the bus's inbox cap) of 8000-character bodies, JSON-escaped at up to
// 12 bytes a character, about 2 MB; node's default spawnSync buffer is 1 MiB and a full buffer kills the child with
// SIGTERM after it already acknowledged the rows (attack A1 finding 1), so the buffer is 32 MiB and any failure of the
// spawn is returned as a FAILURE the caller makes visible instead of silently treating it as "nothing pending".
const MAX_BUFFER = 32 * 1024 * 1024;

function hook(args: string[], stdin: string): { out: any | null; failure: string | null } {
	const r = spawnSync(AGENT, args, { input: stdin, encoding: "utf8", timeout: 15000, maxBuffer: MAX_BUFFER, env: process.env });
	fire({ event: "spawn", args, status: r.status, signal: r.signal, error: r.error ? String(r.error).slice(0, 120) : null,
		stdout_chars: (r.stdout ?? "").length, stdout_head: (r.stdout ?? "").slice(0, 300), stderr_head: (r.stderr ?? "").slice(0, 300) });
	if (r.error || r.signal || r.status !== 0) {
		return { out: null, failure: `aos-agent ${args.join(" ")} ${r.error ? String(r.error) : r.signal ? `killed by ${r.signal}` : `exited ${r.status}`}` };
	}
	const out = (r.stdout ?? "").trim();
	if (!out) return { out: null, failure: null };
	try {
		return { out: JSON.parse(out), failure: null };
	} catch {
		fire({ event: "spawn_unparsed", args, head: out.slice(0, 200) });
		return { out: null, failure: `aos-agent ${args.join(" ")} printed ${out.length} unparsable characters` };
	}
}

// Session identity as the extension context exposes it (probe: does a subagent's session reach the same handlers?).
function sid(ctx: any): Record<string, unknown> {
	try {
		const sm = ctx?.sessionManager;
		return { instance: INSTANCE, pid: process.pid, loaded_at: LOADED_AT, session_file: sm?.getSessionFile?.() ?? null, session_id: sm?.getSessionId?.() ?? null };
	} catch (err) {
		return { sid_error: String(err).slice(0, 80) };
	}
}

export default function aosPi(pi: any) {
	pi.on("session_start", async (_event: any, ctx: any) => {
		fire({ event: "session_start", deliver: DELIVER, stop: STOP, stop_event: STOP_EVENT, home: homedir(), cwd: process.cwd(), agent: AGENT,
			run_file: process.env.AOS_AGENT_RUN_FILE ?? "(default)", state_dir: process.env.AOS_AGENT_STATE_DIR ?? "(default)", ...sid(ctx) });
	});

	pi.on("tool_result", async (event: any, ctx: any) => {
		if (DELIVER === "none") {
			fire({ event: "tool_result", tool: event.toolName, deliver: "none", ...sid(ctx) });
			return undefined;
		}
		const { out, failure } = hook(["inbox", "--hook"], "{}");
		const text: string | undefined = out?.hookSpecificOutput?.additionalContext;
		fire({ event: "tool_result", tool: event.toolName, injected: Boolean(text), chars: text?.length ?? 0, failure, deliver: DELIVER, ...sid(ctx) });
		if (failure) {
			// Visible, never silent: rows the hook acknowledged before it died are gone from the pending set, so the
			// agent and the operator must see that a delivery failed here (attack A1 finding 1).
			return { content: [...(event.content ?? []), { type: "text", text: `[aos] inbound message delivery failed here (${failure}); messages acknowledged by that attempt were not delivered.` }] };
		}
		if (!text) return undefined;
		switch (DELIVER) {
			case "content":
				return { content: [...(event.content ?? []), { type: "text", text }] };
			case "additional":
				return { additionalContext: text };
			case "steer":
				pi.sendUserMessage(text, { deliverAs: "steer" });
				return undefined;
			case "message":
				pi.sendMessage({ customType: "aos-inbound", content: text, display: true }, { deliverAs: "steer" });
				return undefined;
			case "aside":
				pi.sendMessage({ customType: "aos-inbound", content: text, display: true }, { deliverAs: "aside" });
				return undefined;
			default:
				fire({ event: "deliver_unknown", deliver: DELIVER });
				return undefined;
		}
	});

	const stopHandler = async (event: any, ctx: any) => {
		if (STOP_EVENT === "turn_end") {
			// The final answer is a turn with no tool results whose assistant message stopped with "stop": an error,
			// length, aborted or toolUse stop is not the end of the run and must not spend the one continuation
			// (attack A1 finding 6; the runner's own parser accepts only "stop" as a successful end).
			const results = Array.isArray(event?.toolResults) ? event.toolResults.length : 0;
			const stopReason = event?.message?.stopReason ?? null;
			if (results > 0 || stopReason !== "stop") {
				fire({ event: "turn_end", skipped: results > 0 ? "tool results present" : `stopReason ${stopReason}`, n: results, ...sid(ctx) });
				return undefined;
			}
		}
		const { out, failure } = hook(["stop", "--hook"], JSON.stringify({ stop_hook_active: false }));
		const reason: string | undefined = out?.decision === "block" ? out.reason : undefined;
		fire({ event: STOP_EVENT, blocked: Boolean(reason), failure, stop: STOP, ...sid(ctx) });
		if (!reason) return undefined;
		if (STOP === "followup") {
			pi.sendUserMessage(reason, { deliverAs: "followUp" });
			return undefined;
		}
		if (STOP === "boundary") {
			return { entries: [{ type: "custom_message", customType: "aos-stop", content: reason, display: true }], continue: true };
		}
		return undefined;
	};
	if (STOP !== "none") {
		try {
			pi.on(STOP_EVENT, stopHandler);
		} catch (err) {
			fire({ event: "register_failed", what: STOP_EVENT, error: String(err).slice(0, 160) });
		}
	}
	// Observability only: the event stream a runner sees; never acts.
	pi.on("agent_end", async () => fire({ event: "agent_end_seen" }));
}
