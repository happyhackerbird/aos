import { spawnSync } from "node:child_process";

export const protocol = "2.2";

type Context = { sessionManager?: { getSessionId?: () => unknown; getHeader?: () => unknown } };
type ContentBlock = { type: string; [key: string]: unknown };
type ToolResultEvent = {
  content: ContentBlock[];
  isError?: boolean;
  [key: string]: unknown;
};
type TurnEndEvent = {
  message: { role: string; stopReason?: string };
  toolResults: unknown[];
};
type ExtensionAPI = {
  on(name: "session_start", handler: (event: unknown, context: Context) => void): void;
  on(name: "tool_result", handler: (event: ToolResultEvent, context: Context) => ToolResultEvent | undefined): void;
  on(name: "turn_end", handler: (event: TurnEndEvent, context: Context) => void): void;
  on(name: "agent_start" | "agent_end", handler: (event: unknown, context: Context) => void): void;
  sendMessage(message: {customType: string; content: string; display: boolean; attribution: string},
              options: {deliverAs: "aside"}): void;
  sendUserMessage(text: string, options: { deliverAs: "followUp" }): void;
};
type HookResult = { text: string | null; failed: boolean };

const NUDGE_INTERVAL_MS = 10000;
const NUDGE_TEXT = (n: number) => `${n} AOS message(s) are waiting; call mcp__aos_inbox`;

const FAILURE = "[aos] Inbound message delivery failed. Delivery and acknowledgement are uncertain; messages may be delivered again. Use the explicit inbox tool if needed.";
const STOP_FAILURE = "[aos] Final message check failed. Answer any acknowledged request you have not answered, or explain why not, then restate your full final report. This is the single run-level continuation; requests received during it get no further continuation.";

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hook(kind: "inbox" | "stop", active = false): HookResult {
  let result;
  try {
    result = spawnSync("/usr/local/bin/aos-agent", [kind, "--hook"], {
      input: JSON.stringify(kind === "stop" ? { stop_hook_active: active } : {}),
      encoding: "utf8",
      timeout: 15000,
      maxBuffer: 32 * 1024 * 1024,
    });
  } catch {
    return { text: null, failed: true };
  }
  const failed = Boolean(result.error || result.signal || result.status !== 0 ||
    (result.stderr ?? "").split(/\r?\n/).includes("AOS_AGENT_HOOK_FAILURE"));
  const raw = (result.stdout ?? "").trim();
  if (!raw) return { text: null, failed: failed || Boolean((result.stderr ?? "").trim()) };
  try {
    const output: unknown = JSON.parse(raw);
    let text: unknown;
    if (object(output)) {
      if (kind === "inbox") {
        const context = output.hookSpecificOutput;
        if (object(context) && context.hookEventName === "PostToolUse") text = context.additionalContext;
      } else if (output.decision === "block") {
        text = output.reason;
      }
    }
    if (typeof text !== "string" || !text.trim()) return { text: null, failed: true };
    return { text, failed };
  } catch {
    return { text: null, failed: true };
  }
}

function peek(): {status: number | null; stdout: string; stderr: string} {
  return spawnSync("/usr/local/bin/aos-agent", ["peek"], {encoding: "utf8", timeout: 15000});
}

export default function adapter(pi: ExtensionAPI): void {
  const interactive = process.env.AOS_INTERACTIVE === "1";
  let started = false;
  let owner: string | null = null;
  let delivered = false;
  let timer: ReturnType<typeof setInterval> | null = null;
  let nudged = false;
  let retired = false;
  const clearTimer = () => {
    if (timer !== null) clearInterval(timer);
    timer = null;
  };
  const tick = () => {
    const p = peek();
    if (/no run credential/.test(p.stderr)) {
      clearTimer();
      return;
    }
    if (/bus 409/.test(p.stderr)) {
      clearTimer();
      retired = true;
      return;
    }
    if (p.status !== 0) return;
    let n: number;
    try {
      n = Number(JSON.parse(p.stdout).pending);
    } catch {
      return;
    }
    if (n > 0 && !nudged) {
      pi.sendMessage({customType: "aos-nudge", content: NUDGE_TEXT(n), display: true, attribution: "agent"},
                     {deliverAs: "aside"});
      nudged = true;
      clearTimer();
    }
  };
  pi.on("session_start", (_event, context) => {
    if (started) return;
    started = true;
    const header = context.sessionManager?.getHeader?.();
    if (!object(header) || header.parentSession != null || header.parentSessionId != null) return;
    const id = context.sessionManager?.getSessionId?.();
    owner = typeof id === "string" && id.trim() ? id : null;
  });
  pi.on("tool_result", (event, context) => {
    if (owner === null || context.sessionManager?.getSessionId?.() !== owner) return;
    const result = hook("inbox");
    if (result.text === null && !result.failed) return;
    const content = [...event.content];
    if (result.text !== null) content.push({ type: "text", text: result.text });
    if (result.failed) content.push({ type: "text", text: FAILURE });
    return { ...event, content };
  });
  pi.on("turn_end", (event, context) => {
    if (interactive) return;
    if (owner === null || context.sessionManager?.getSessionId?.() !== owner ||
        event.message?.role !== "assistant" || event.message.stopReason !== "stop" ||
        !Array.isArray(event.toolResults) || event.toolResults.length !== 0) return;
    const first = hook("stop", delivered);
    if (delivered) return;
    let reason = first.text;
    const failed = first.failed;
    if (reason === null && failed) {
      const retry = hook("stop", false);
      reason = retry.text;
    }
    if (reason === null && !failed) return;
    if (failed) reason = reason === null ? STOP_FAILURE : reason + "\n\n" + STOP_FAILURE;
    delivered = true;
    if (reason !== null) pi.sendUserMessage(reason, { deliverAs: "followUp" });
  });
  pi.on("agent_start", (_event, context) => {
    if (owner === null || context.sessionManager?.getSessionId?.() !== owner) return;
    nudged = false;
    clearTimer();
  });
  pi.on("agent_end", (_event, context) => {
    if (!interactive) return;
    if (owner === null || context.sessionManager?.getSessionId?.() !== owner) return;
    if (retired || timer !== null) return;
    timer = setInterval(tick, NUDGE_INTERVAL_MS);
  });
}
