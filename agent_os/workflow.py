"""The repository workflow file, aos-workflow.toml: read only at the default-branch commit the repository policy
uses (policy.resolve_default), never from a worktree or a task branch. Design:
docs/design/2026-10-15-repo-workflow-file.md."""
from __future__ import annotations

import tomllib
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from agent_os import policy

WORKFLOW_PATH = "aos-workflow.toml"
PROMPT_MAX_BYTES = 4096
KEYS = frozenset({"test_command", "acceptance", "prompt"})
_PREFIX = "workflow: aos-workflow.toml: "


class WorkflowError(ValueError):
    pass


@dataclass(frozen=True)
class Workflow:
    test_command: str | None
    acceptance: tuple[str, ...]
    prompt: str | None
    sha: str
    ref: str


def _error(what: str) -> WorkflowError:
    return WorkflowError(_PREFIX + what)


def _has_control(text: str, allowed: tuple[str, ...]) -> bool:
    return any(unicodedata.category(c) == "Cc" and c not in allowed for c in text)


def parse_workflow(text: str) -> tuple[str | None, tuple[str, ...], str | None]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise _error(str(exc)) from None
    unknown = sorted(set(data) - KEYS)
    if unknown:
        raise _error(f"unknown key {unknown[0]}")

    test_command: str | None = None
    if "test_command" in data:
        value = data["test_command"]
        if not isinstance(value, str):
            raise _error("test_command must be a string")
        test_command = value.strip()
        if not test_command:
            raise _error("test_command is blank")
        if _has_control(test_command, ("\t",)):
            raise _error("test_command has a control character")

    acceptance: tuple[str, ...] = ()
    if "acceptance" in data:
        items = data["acceptance"]
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise _error("acceptance must be a list of strings")
        if not items:
            raise _error("acceptance is empty; omit the key for no default")
        stripped = []
        for index, item in enumerate(items):
            entry = item.strip()
            if not entry:
                raise _error(f"acceptance[{index}] is blank")
            if _has_control(entry, ("\t",)):
                raise _error(f"acceptance[{index}] has a control character")
            stripped.append(entry)
        acceptance = tuple(stripped)

    prompt: str | None = None
    if "prompt" in data:
        value = data["prompt"]
        if not isinstance(value, str):
            raise _error("prompt must be a string")
        prompt = value.replace("\r\n", "\n").strip()
        if not prompt:
            raise _error("prompt is blank")
        if _has_control(prompt, ("\n", "\t")):
            raise _error("prompt has a control character")
        size = len(prompt.encode("utf-8"))
        if size > PROMPT_MAX_BYTES:
            raise _error(f"prompt is {size} bytes; the cap is {PROMPT_MAX_BYTES}")
    return test_command, acceptance, prompt


def read_workflow(source: str, default: policy.Default) -> Workflow | None:
    if default.state == "empty":
        return None
    try:
        _, listing = policy.git_output(source, "ls-tree", "--full-tree", default.sha, "--", WORKFLOW_PATH)
    except policy.PolicyError as exc:
        raise WorkflowError("workflow: " + str(exc)) from None
    if not listing.strip():
        return None
    info, name = listing.splitlines()[0].split(b"\t", 1)
    mode, kind, blob_id = info.split()
    if name != WORKFLOW_PATH.encode() or kind != b"blob" or mode not in (b"100644", b"100755"):
        raise WorkflowError("workflow: aos-workflow.toml is not a regular file")
    try:
        _, blob = policy.git_output(source, "cat-file", "blob", blob_id.decode())
    except policy.PolicyError as exc:
        raise WorkflowError("workflow: " + str(exc)) from None
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _error(str(exc)) from None
    return Workflow(*parse_workflow(text), sha=sha256(blob).hexdigest(), ref=default.ref)


def run_acceptance(run: dict[str, Any]) -> list[str]:
    """Checks frozen when this run was claimed; never reconstruct historical defaults."""
    commands = run.get("effective_acceptance")
    if (not isinstance(commands, list) or not commands
            or any(not isinstance(command, str) or not command.strip() for command in commands)):
        raise WorkflowError("acceptance: the run has no valid execution snapshot")
    return list(commands)


def execution_snapshot(task: dict[str, Any], workflow: Workflow | None,
                       default: policy.Default) -> dict[str, Any]:
    commands = task.get("acceptance") or []
    source = "task"
    if not commands:
        if workflow is not None and workflow.acceptance:
            commands, source = list(workflow.acceptance), "workflow.acceptance"
        elif workflow is not None and workflow.test_command:
            commands, source = [workflow.test_command], "workflow.test_command"
    if (task.get("execution") == "interactive" and source == "task" and workflow is not None
            and workflow.test_command and workflow.test_command not in commands):
        commands = [*commands, workflow.test_command]
        source = "task+workflow.test_command"
    snapshot = {
        "effective_acceptance": commands,
        "acceptance_source": source,
        "workflow_commit": default.sha if workflow is not None else None,
        "workflow_ref": workflow.ref if workflow is not None else None,
        "workflow_sha": workflow.sha if workflow is not None else None,
    }
    try:
        snapshot["effective_acceptance"] = run_acceptance(snapshot)
    except WorkflowError:
        raise WorkflowError("acceptance: no task commands or repository workflow defaults") from None
    return snapshot
