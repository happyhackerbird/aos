import { spawn, spawnSync, type ChildProcess } from "node:child_process";

export const protocol = "2.6";

type Context = {
  isIdle?: () => boolean;
  sessionManager?: {
    getSessionId?: () => unknown;
    getHeader?: () => unknown;
    getSessionFile?: () => unknown;
  };
};
type ContentBlock = { type: string; text?: string; [key: string]: unknown };
type ToolResultEvent = {
  toolName?: string;
  content: ContentBlock[];
  isError?: boolean;
  [key: string]: unknown;
};
type TurnEndEvent = {
  message: { role: string; stopReason?: string };
  toolResults: unknown[];
};
type ContextEvent = {
  messages?: Array<{
    role?: string;
    customType?: string;
    details?: { ids?: unknown };
    [key: string]: unknown;
  }>;
};
type QueuedMessage = {
  id: string;
  kind?: unknown;
  task_id?: unknown;
  reply_to?: unknown;
  body?: unknown;
  framed: string;
};
type ExtensionAPI = {
  on(name: "session_start" | "session_shutdown", handler: (event: unknown, context: Context) => void): void;
  on(name: "session_switch", handler: (event: { previousSessionFile?: unknown }, context: Context) => void): void;
  on(name: "context", handler: (event: ContextEvent, context: Context) => void): void;
  on(name: "tool_call", handler: (event: unknown, context: Context) => void): void;
  on(name: "tool_result", handler: (event: ToolResultEvent, context: Context) => ToolResultEvent | undefined): void;
  on(name: "turn_end", handler: (event: TurnEndEvent, context: Context) => void): void;
  on(name: "agent_start" | "agent_end", handler: (event: unknown, context: Context) => void): void;
  sendMessage(
    message: {
      customType: string;
      content: string;
      display: boolean;
      attribution: string;
      details?: Record<string, unknown>;
    },
    options: { deliverAs: "aside" }
  ): void;
  sendUserMessage(text: string, options: { deliverAs: "followUp" }): void;
  [key: string]: unknown;
};
type HookResult = { text: string | null; failed: boolean };

const DEFAULT_QUEUE_INTERVAL_MS = 1000;
const DEFAULT_ACK_NOTE =
  "[aos] Delivery is best-effort: a message may be delivered again. Answer a request with aos_reply.";

const FAILURE =
  "[aos] Inbound message delivery failed. Delivery is uncertain and nothing was acknowledged; messages may be delivered again. Use the explicit inbox tool if needed.";
const STOP_FAILURE =
  "[aos] Final message check failed. Answer any request delivered to you that you have not answered, or explain why not, then restate your full final report. This is the single run-level continuation; requests received during it get no further continuation.";

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
  const failed = Boolean(
    result.error ||
      result.signal ||
      result.status !== 0 ||
      (result.stderr ?? "").split(/\r?\n/).includes("AOS_AGENT_HOOK_FAILURE")
  );
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

function receiver(kind: "receiver-start" | "receiver-end", owner: string, reason?: string): boolean {
  try {
    const result = spawnSync("/usr/local/bin/aos-agent", [kind, "--hook"], {
      input: JSON.stringify({ session_id: owner, reason }),
      encoding: "utf8",
      timeout: 2500,
      maxBuffer: 8192,
    });
    if (!result.error && !result.signal && result.status === 0) return true;
  } catch {}
  process.stderr.write("[aos] Receiver lifecycle transition failed; bus state is unconfirmed.\n");
  return false;
}

function root(context: Context): boolean {
  const header = context.sessionManager?.getHeader?.();
  return object(header) && header.parentSession == null && header.parentSessionId == null;
}

function extractFrameIds(content: ContentBlock[] | undefined): string[] {
  if (!Array.isArray(content)) return [];
  const ids: string[] = [];
  const re = /<(?:agent-request|agent-notice|operator-message)\s+id="([^"]+)"/g;
  for (const block of content) {
    if (typeof block?.text !== "string") continue;
    let m: RegExpExecArray | null;
    while ((m = re.exec(block.text)) !== null) {
      if (m[1]) ids.push(m[1]);
    }
  }
  return ids;
}

export default function adapter(pi: ExtensionAPI): void {
  const interactive = process.env.AOS_INTERACTIVE === "1";
  const automatic = process.env.AOS_AGENT_AUTOMATIC === "1";
  const omp =
    process.env.AOS_PI_FLAVOUR === "omp" ||
    (process.env.AOS_PI_FLAVOUR !== "pi" &&
      (Boolean((process.versions as Record<string, unknown> | undefined)?.bun) ||
        Boolean(object(pi) && "runtime" in pi)));
  const rawInterval = Number(process.env.AOS_OMP_QUEUE_INTERVAL_MS);
  const queueIntervalMs = rawInterval > 0 ? rawInterval : DEFAULT_QUEUE_INTERVAL_MS;

  let started = false;
  let owner: string | null = null;
  let ownerFile: string | null = null;
  let delivered = false;
  let timer: ReturnType<typeof setInterval> | null = null;
  let fetchChild: ChildProcess | null = null;
  let retired = false;
  let agentRunning = false;
  let activeTools = 0;
  let lastContext: Context | null = null;
  let pollFailed = false;
  let ackNote = DEFAULT_ACK_NOTE;

  const known = new Map<string, QueuedMessage>();
  const queue: QueuedMessage[] = [];
  const preparedIds = new Set<string>();
  const pendingPrepare = new Map<string, QueuedMessage>();
  let inFlight: string[] | null = null;

  const clearTimer = () => {
    if (timer !== null) clearInterval(timer);
    timer = null;
  };

  const killFetch = () => {
    if (fetchChild !== null) {
      try {
        fetchChild.kill();
      } catch {}
      fetchChild = null;
    }
  };

  const retireAll = () => {
    retired = true;
    clearTimer();
    killFetch();
    queue.length = 0;
    inFlight = null;
    pendingPrepare.clear();
  };

  const selectOwner = (context: Context) => {
    if (started) return;
    started = true;
    if (!root(context)) return;
    const id = context.sessionManager?.getSessionId?.();
    owner = typeof id === "string" && id.trim() ? id : null;
    const file = context.sessionManager?.getSessionFile?.();
    ownerFile = typeof file === "string" && file.trim() ? file : null;
  };

  const owns = (context: Context) =>
    !retired && owner !== null && root(context) && context.sessionManager?.getSessionId?.() === owner;

  const runPrepare = (rows: QueuedMessage[]): void => {
    if (retired || owner === null || rows.length === 0) return;
    let res;
    try {
      res = spawnSync("/usr/local/bin/aos-agent", ["prepare", "--hook"], {
        input: JSON.stringify({ messages: rows }),
        encoding: "utf8",
        timeout: 15000,
        maxBuffer: 32 * 1024 * 1024,
      });
    } catch {
      for (const r of rows) {
        if (!preparedIds.has(r.id)) pendingPrepare.set(r.id, r);
      }
      pollFailed = true;
      return;
    }
    const failed = Boolean(
      res.error ||
        res.signal ||
        res.status !== 0 ||
        (res.stderr ?? "").split(/\r?\n/).includes("AOS_AGENT_HOOK_FAILURE")
    );
    let confirmed = new Set<string>();
    try {
      const parsed: unknown = JSON.parse((res.stdout ?? "").trim() || "{}");
      if (object(parsed) && Array.isArray(parsed.prepared_ids)) {
        confirmed = new Set(parsed.prepared_ids.filter((x): x is string => typeof x === "string"));
      }
    } catch {
      pollFailed = true;
    }
    for (const r of rows) {
      if (confirmed.has(r.id)) {
        preparedIds.add(r.id);
        pendingPrepare.delete(r.id);
      } else {
        pendingPrepare.set(r.id, r);
      }
    }
    if (failed || confirmed.size < rows.length) pollFailed = true;
  };

  const retryPendingPrepare = () => {
    if (retired || owner === null || pendingPrepare.size === 0) return;
    runPrepare([...pendingPrepare.values()]);
  };

  const ingestQueueOutput = (stdout: string): boolean => {
    try {
      const parsed: unknown = JSON.parse(stdout.trim());
      if (!object(parsed) || !Array.isArray(parsed.messages)) return false;
      if (typeof parsed.ack_note === "string" && parsed.ack_note.trim()) {
        ackNote = parsed.ack_note;
      }
      for (const item of parsed.messages) {
        if (!object(item) || typeof item.id !== "string" || !item.id.trim()) continue;
        const id = item.id;
        if (known.has(id) || preparedIds.has(id)) continue;
        const framed = typeof item.framed === "string" ? item.framed : String(item.body ?? "");
        const msg: QueuedMessage = {
          id,
          kind: item.kind,
          task_id: item.task_id,
          reply_to: item.reply_to,
          body: item.body,
          framed,
        };
        known.set(id, msg);
        queue.push(msg);
      }
      return true;
    } catch {
      return false;
    }
  };

  const flushQueue = (atBoundary = false) => {
    if (!omp || retired || owner === null || inFlight !== null || queue.length === 0) return;
    const isIdle = !agentRunning || Boolean(lastContext?.isIdle?.());
    if (!isIdle && activeTools <= 0 && !atBoundary) return;
    const batch = queue.splice(0, queue.length);
    const ids = batch.map((m) => m.id);
    inFlight = ids;
    const content = batch.map((m) => m.framed).join("\n\n") + "\n\n" + ackNote;
    pi.sendMessage(
      {
        customType: "aos-peer",
        content,
        display: true,
        attribution: "agent",
        details: { ids },
      },
      { deliverAs: "aside" }
    );
  };

  const handleQueueError = (stderr: string): boolean => {
    if (/no run credential/.test(stderr)) {
      clearTimer();
      return true;
    }
    if (/bus 409/.test(stderr)) {
      retireAll();
      return true;
    }
    pollFailed = true;
    return false;
  };

  const syncQueue = () => {
    if (!omp || retired || owner === null) return;
    retryPendingPrepare();
    if (inFlight !== null && queue.length >= 20) return;
    let res;
    try {
      res = spawnSync("/usr/local/bin/aos-agent", ["queue"], {
        encoding: "utf8",
        timeout: 15000,
        maxBuffer: 32 * 1024 * 1024,
      });
    } catch {
      pollFailed = true;
      return;
    }
    if (res.error || res.signal || res.status !== 0) {
      handleQueueError(res.stderr ?? "");
      return;
    }
    if (!ingestQueueOutput(res.stdout ?? "")) {
      pollFailed = true;
      return;
    }
    pollFailed = false;
  };

  const poll = () => {
    if (!omp || retired || owner === null || fetchChild !== null) return;
    if (inFlight !== null && queue.length >= 20) return;
    let child: ChildProcess;
    try {
      child = spawn("/usr/local/bin/aos-agent", ["queue"], { stdio: ["ignore", "pipe", "pipe"] });
    } catch {
      pollFailed = true;
      return;
    }
    fetchChild = child;
    let stdout = "";
    let stderr = "";
    child.stdout?.on("data", (chunk: Buffer | string) => {
      stdout += chunk.toString();
    });
    child.stderr?.on("data", (chunk: Buffer | string) => {
      stderr += chunk.toString();
    });
    child.on("error", () => {
      if (fetchChild === child) fetchChild = null;
      if (!retired) pollFailed = true;
    });
    child.on("close", (code, signal) => {
      if (fetchChild === child) fetchChild = null;
      if (retired || owner === null) return;
      if (code !== 0 || signal) {
        handleQueueError(stderr);
        return;
      }
      if (!ingestQueueOutput(stdout)) {
        pollFailed = true;
        return;
      }
      pollFailed = false;
      retryPendingPrepare();
      flushQueue();
    });
  };

  const armTimer = () => {
    if (!omp || !automatic || retired || owner === null) return;
    if (timer === null) {
      timer = setInterval(poll, queueIntervalMs);
    }
  };

  pi.on("session_start", (_event, context) => {
    if (started) return;
    selectOwner(context);
    if (owner !== null && !receiver("receiver-start", owner)) {
      retireAll();
      return;
    }
    if (automatic && omp && owns(context)) {
      lastContext = context;
      armTimer();
      poll();
    }
  });

  pi.on("session_shutdown", (_event, context) => {
    selectOwner(context);
    if (!owns(context)) return;
    const oldOwner = owner!;
    retireAll();
    receiver("receiver-end", oldOwner);
  });

  pi.on("session_switch", (event, context) => {
    const id = context.sessionManager?.getSessionId?.();
    if (
      retired ||
      owner === null ||
      ownerFile === null ||
      event.previousSessionFile !== ownerFile ||
      typeof id !== "string" ||
      !id.trim() ||
      id === owner
    )
      return;
    const oldOwner = owner;
    retireAll();
    receiver("receiver-end", oldOwner, "native_owner_replaced");
  });

  if (!automatic) return;

  pi.on("context", (event, context) => {
    if (!omp || retired || !owns(context)) return;
    lastContext = context;
    retryPendingPrepare();
    const messages = Array.isArray(event?.messages) ? event.messages : [];
    const observedIds = new Set<string>();
    const toPrepare: QueuedMessage[] = [];
    for (const msg of messages) {
      if (msg?.customType !== "aos-peer" || !Array.isArray(msg?.details?.ids)) continue;
      for (const rawId of msg.details.ids) {
        if (typeof rawId !== "string") continue;
        observedIds.add(rawId);
        if (!preparedIds.has(rawId) && known.has(rawId)) {
          toPrepare.push(known.get(rawId)!);
        }
      }
    }
    if (toPrepare.length > 0) {
      runPrepare(toPrepare);
    }
    if (inFlight !== null && inFlight.every((id) => observedIds.has(id) || preparedIds.has(id))) {
      inFlight = null;
      flushQueue();
      poll();
    }
  });

  pi.on("tool_call", (_event, context) => {
    if (!omp || retired || !owns(context)) return;
    lastContext = context;
    activeTools++;
    flushQueue();
  });

  pi.on("tool_result", (event, context) => {
    if (retired || !owns(context)) return;
    if (!omp) {
      const result = hook("inbox");
      if (result.text === null && !result.failed) return;
      const content = [...event.content];
      if (result.text !== null) content.push({ type: "text", text: result.text });
      if (result.failed) content.push({ type: "text", text: FAILURE });
      return { ...event, content };
    }
    lastContext = context;
    if (activeTools > 0) activeTools--;
    const deliveredByTool = extractFrameIds(event?.content);
    if (deliveredByTool.length > 0) {
      for (const id of deliveredByTool) {
        preparedIds.add(id);
        pendingPrepare.delete(id);
      }
      for (let i = queue.length - 1; i >= 0; i--) {
        if (preparedIds.has(queue[i].id)) queue.splice(i, 1);
      }
    }
    if (inFlight === null && queue.length === 0) {
      syncQueue();
    } else {
      retryPendingPrepare();
    }
    if (inFlight === null && queue.length > 0) {
      flushQueue(true);
    }
    if (pollFailed) {
      pollFailed = false;
      const content = [...(event.content ?? []), { type: "text", text: FAILURE }];
      return { ...event, content };
    }
    return;
  });

  pi.on("turn_end", (event, context) => {
    if (retired || !owns(context)) return;
    if (omp) {
      lastContext = context;
      retryPendingPrepare();
      if (inFlight === null && queue.length > 0) {
        flushQueue(true);
      }
    }
    if (interactive) return;
    if (
      event.message?.role !== "assistant" ||
      event.message.stopReason !== "stop" ||
      !Array.isArray(event.toolResults) ||
      event.toolResults.length !== 0
    )
      return;
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
    if (retired || !owns(context)) return;
    if (!omp) return;
    lastContext = context;
    agentRunning = true;
    armTimer();
  });

  pi.on("agent_end", (_event, context) => {
    if (retired || !owns(context)) return;
    if (!omp) return;
    lastContext = context;
    agentRunning = false;
    activeTools = 0;
    armTimer();
    flushQueue();
    setTimeout(() => {
      if (!retired && owner !== null) {
        flushQueue();
        poll();
      }
    }, 20);
  });
}
