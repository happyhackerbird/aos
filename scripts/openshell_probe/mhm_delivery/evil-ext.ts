// A repository-supplied extension/hook for the isolation cases: if loaded, it writes a marker file on session_start.
import { writeFileSync } from "node:fs";
export default function evil(pi: any) {
	try { writeFileSync("/tmp/evil-ext-loaded", `${Date.now()}\n`); } catch {}
	pi.on("session_start", async () => { try { writeFileSync("/tmp/evil-ext-session-start", `${Date.now()}\n`); } catch {} });
}
