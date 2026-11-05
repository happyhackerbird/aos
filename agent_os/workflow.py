"""The repository workflow file, aos-workflow.toml: read only at the default-branch commit the repository policy
uses (policy.resolve_default), never from a worktree or a task branch. Design:
docs/design/2026-10-15-repo-workflow-file.md."""
from __future__ import annotations

import re
import tomllib
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from agent_os import policy

WORKFLOW_PATH = "aos-workflow.toml"
PROMPT_MAX_BYTES = 4096
KEYS = frozenset({"test_command", "acceptance", "prompt", "protected", "watch", "required_checks", "baseline"})
CHECK_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")  # the bus's rule (store.CHECK_NAME); a workflow is read first
TEST_CHANGES_CHECK = "test-changes"  # added by AOS when tests vanish (U3); recorded, never configured
# Always locked, whatever `protected` says: AOS owns their meaning, so a change to either is a coordinator commit on
# the default branch, never an agent's (verification gates U1).
ALWAYS_LOCKED = (policy.POLICY_PATH, WORKFLOW_PATH)
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
    protected: tuple[str, ...] = ()   # git glob patterns an agent's result may not touch (ALWAYS_LOCKED added)
    watch: tuple[str, ...] = ()       # git glob patterns the change report lists by name
    required_checks: tuple[str, ...] = ()  # the repository's default checks before accept or merge (U2)
    baseline: bool = False            # run the acceptance commands before the agent too (U3)


def locked_patterns(protected: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The patterns a run's result is checked against: the two AOS files first, then the repository's own."""
    return tuple(dict.fromkeys((*ALWAYS_LOCKED, *protected)))


def _error(what: str) -> WorkflowError:
    return WorkflowError(_PREFIX + what)


def _has_control(text: str, allowed: tuple[str, ...]) -> bool:
    return any(unicodedata.category(c) == "Cc" and c not in allowed for c in text)


def _patterns(data: dict[str, Any], key: str) -> tuple[str, ...]:
    """A list of repository-relative git glob patterns (matched as `:(glob)<pattern>`): no leading `/` or `:`, no
    empty, `.` or `..` component, no blank and no control character."""
    items = data[key]
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise _error(f"{key} must be a list of strings")
    if not items:
        raise _error(f"{key} is empty; omit the key for none")
    patterns = []
    for index, item in enumerate(items):
        entry = item.strip()
        if not entry:
            raise _error(f"{key}[{index}] is blank")
        if _has_control(entry, ()):
            raise _error(f"{key}[{index}] has a control character")
        if entry.startswith("/"):
            raise _error(f"{key}[{index}] starts with /; patterns are relative to the repository root")
        if entry.startswith(":"):
            raise _error(f"{key}[{index}] starts with :; pathspec magic is not accepted")
        if any(part in ("", ".", "..") for part in entry.removesuffix("/").split("/")):
            raise _error(f"{key}[{index}] has an empty, . or .. path component")
        patterns.append(entry)
    return tuple(patterns)


def _check_names(data: dict[str, Any]) -> tuple[str, ...]:
    items = data["required_checks"]
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise _error("required_checks must be a list of strings")
    if not items:
        raise _error("required_checks is empty; omit the key for none")
    for index, item in enumerate(items):
        if not CHECK_NAME.fullmatch(item):
            raise _error(f"required_checks[{index}] is not a check name ([a-z0-9][a-z0-9-]{{0,39}})")
        if item == TEST_CHANGES_CHECK:
            raise _error(f"required_checks[{index}]: {TEST_CHANGES_CHECK} is added by AOS when tests are lost; "
                         "turn on baseline instead")
    names = tuple(dict.fromkeys(items))
    if len(names) > 20:
        raise _error("required_checks holds more than 20 names")
    return names


def parse_workflow(text: str) -> dict[str, Any]:
    """The workflow's fields by name, the keyword arguments of Workflow other than sha and ref."""
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
    return {"test_command": test_command, "acceptance": acceptance, "prompt": prompt,
            "protected": _patterns(data, "protected") if "protected" in data else (),
            "watch": _patterns(data, "watch") if "watch" in data else (),
            "required_checks": _check_names(data) if "required_checks" in data else (),
            "baseline": _boolean(data, "baseline")}


def _boolean(data: dict[str, Any], key: str) -> bool:
    if key not in data:
        return False
    if not isinstance(data[key], bool):
        raise _error(f"{key} must be true or false")
    return data[key]


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
    return Workflow(**parse_workflow(text), sha=sha256(blob).hexdigest(), ref=default.ref)


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
    # Verification gates U2: the repository's default checks, then the task's own; sent with the claim only when
    # there are any, so a bus without the field still takes every other claim.
    required = list(dict.fromkeys([*(workflow.required_checks if workflow is not None else ()),
                                   *(task.get("required_checks") or [])]))
    if required:
        snapshot["required_checks"] = required
    return snapshot
