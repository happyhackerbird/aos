// pi-probe: the mechanism probe's own extension for Pi and omp (one file, no imports beyond node).
//
// Loaded by an explicit `-e <path>` under the runner's `--no-extensions`. It is the "hooks" half of
// the probe: Pi-family harnesses have no hooks file, an extension's event handlers are the hooks.
//   session_start      -> one fire-log line, plus the boundary read from the extension's own process
//                         (an HTTPS fetch, a /usr write, the token entries in process.env, a state
//                         write under ~/.local/state) so the record shows Landlock and the network
//                         policy binding in-process Node calls, not only shell children
//   tool_call          -> logs every call; blocks a bash command mentioning EXT-DENY-ME
//   tool_result        -> appends a note to a bash result mentioning EXT-NOTE-ME
//   before_subagent_spawn (omp only; registered in a try) -> logs the spawn's agent and model
//   tool probe_echo    -> a custom tool, so a loaded extension is visible as a tool in the stream
// The fire log is ~/.local/share/pi-probe/fires.jsonl: where HOME lands per flavour is itself a
// measurement (Pi: the sandbox's /sandbox; omp: the runner's HOME=/tmp/pi-home).
import { appendFileSync, mkdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

const HOME = homedir();
const LOG = join(HOME, ".local", "share", "pi-probe", "fires.jsonl");
const DENY = "EXT-DENY-ME";
const NOTE = "EXT-NOTE-ME";

function fire(line: Record<string, unknown>): void {
	try {
		mkdirSync(dirname(LOG), { recursive: true });
		appendFileSync(LOG, `${JSON.stringify({ t: Date.now() / 1000, ...line })}\n`);
	} catch {
		// never fail the session on logging
	}
}

async function boundary(): Promise<Record<string, unknown>> {
	const out: Record<string, unknown> = { home: HOME, cwd: process.cwd(), pid: process.pid, argv0: process.argv[0] };
	out.token_entries = Object.keys(process.env).filter((k) => k === "CLAUDE_CODE_OAUTH_TOKEN").length;
	out.env_keys_of_interest = Object.keys(process.env).filter((k) => /^(HOME|PI_|OMP_|CODEX_HOME|CLAUDE_CONFIG_DIR)/.test(k)).sort();
	try {
		const res = await fetch("https://example.com", { signal: AbortSignal.timeout(8000) });
		out.fetch = `status ${res.status} (unexpected)`;
	} catch (err) {
		const e = err as { code?: string; cause?: { code?: string; message?: string }; name?: string; message?: string };
		out.fetch = `error ${e.name ?? ""} ${e.code ?? e.cause?.code ?? ""} ${(e.cause?.message ?? e.message ?? "").slice(0, 120)}`.trim();
	}
	try {
		writeFileSync("/usr/pi-probe-mark", "x");
		out.usr_write = "ok (unexpected)";
	} catch (err) {
		const e = err as { code?: string; errno?: number };
		out.usr_write = `${e.code ?? "?"} errno ${e.errno ?? "?"}`;
	}
	try {
		const state = join(HOME, ".local", "state", "pi-probe");
		mkdirSync(state, { recursive: true });
		writeFileSync(join(state, "mark"), "ok\n");
		out.state_write = `ok ${state}`;
	} catch (err) {
		const e = err as { code?: string };
		out.state_write = `${e.code ?? "?"}`;
	}
	return out;
}

export default function probe(pi: any) {
	pi.on("session_start", async () => {
		fire({ event: "session_start", boundary: await boundary() });
	});
	pi.on("tool_call", async (event: any) => {
		const input = JSON.stringify(event.input ?? {});
		fire({ event: "tool_call", tool: event.toolName, input_head: input.slice(0, 160) });
		if (event.toolName === "bash" && input.includes(DENY)) {
			fire({ event: "tool_call", tool: event.toolName, decision: "block" });
			return { block: true, reason: `[pi-probe] denied by the probe extension (${DENY})` };
		}
		return undefined;
	});
	pi.on("tool_result", async (event: any) => {
		const input = JSON.stringify(event.input ?? {});
		if (event.toolName === "bash" && input.includes(NOTE)) {
			fire({ event: "tool_result", tool: event.toolName, decision: "note" });
			return { content: [...(event.content ?? []), { type: "text", text: "[pi-probe] note appended by the probe extension" }] };
		}
		return undefined;
	});
	try {
		pi.on("before_subagent_spawn", async (event: any) => {
			const head = JSON.stringify(event ?? {}).slice(0, 300);
			fire({ event: "before_subagent_spawn", agent: event?.agentName ?? event?.agent ?? null, model: event?.model ?? null, head });
			return undefined;
		});
	} catch (err) {
		fire({ event: "register_failed", what: "before_subagent_spawn", error: String(err).slice(0, 120) });
	}
	try {
		pi.registerTool({
			name: "probe_echo",
			label: "Probe echo",
			description: "Mechanism probe: echoes its text back with the marker PROBE-ECHO-OK.",
			parameters: { type: "object", properties: { text: { type: "string", description: "text to echo" } }, required: ["text"], additionalProperties: false },
			async execute(_id: string, params: { text: string }) {
				fire({ event: "probe_echo", text: String(params?.text ?? "").slice(0, 80) });
				return { content: [{ type: "text", text: `PROBE-ECHO-OK ${params?.text ?? ""}` }], details: undefined };
			},
		});
	} catch (err) {
		fire({ event: "register_failed", what: "probe_echo", error: String(err).slice(0, 160) });
	}
}
