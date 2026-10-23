// coload-replace: a co-loaded operator extension that REPLACES a bash result's content (the probe for D49's
// composition claim: F160 measured two APPENDING writers; this one drops the previous content and writes its own).
// On a bash result whose command mentions EXT-REPLACE-ME the handler returns a fresh content array with one text block.
export default function probe(pi: any) {
	pi.on("tool_result", async (event: any) => {
		const input = JSON.stringify(event.input ?? {});
		if (event.toolName === "bash" && input.includes("EXT-REPLACE-ME")) {
			return { content: [{ type: "text", text: "[pi-replace] content replaced by the probe extension" }] };
		}
		return undefined;
	});
}
