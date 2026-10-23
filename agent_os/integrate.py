"""aos integrate: merge a task's branch onto the current base branch in a
scratch worktree, run the combined check, and report the result to the bus.

Never pushes, never commits outside the scratch worktree, never touches the
task's own worktree or branch, and never changes the source's checked-out
branch. Reuses agent_os.harness's environment loader; never imports
agent_os.store or the bus's own HTTP layer (the bus row returned by the client
is the only interface). Merged checks run only through
`agent_os.openshell_integrate` in an OpenShell sandbox.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
from typing import IO, Any

import httpx

from agent_os import gitops, harness, openshell_integrate, openshell_runner, openshell_services, policy
from agent_os import workflow as workflow_file

GATEWAY_UNREACHABLE = "openshell gateway is not reachable; refusing to run unconfined"


def _gateway_reachable() -> bool:
    try:
        return openshell_runner.openshell("status", check=False, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _has_origin(source: str) -> bool:
    result = gitops.git_rc("remote", cwd=source)
    return "origin" in result.stdout.split()


def _remove_scratch(source: str, scratch: str, worktree_root: str, repo: str) -> None:
    """Removes any leftover at `scratch`.

    C3: the guard on the path's name and parent -- its last component starts
    with `.integrate-` and its parent is exactly `<worktree_root>/<repo>` --
    runs BEFORE any git call, never after. `git worktree remove --force`
    follows a symlink to whatever it points at, so a symlink planted at the
    scratch path (pointing at a task's own worktree, say) must never reach
    that call: once the guard recognizes the path, a symlink there is removed
    with `os.unlink` on the link itself and nothing else is done to it. Only
    a real directory goes to `git worktree remove --force`, `git worktree
    prune`, and -- if something remains, no longer a worktree git recognizes
    -- `shutil.rmtree`.
    """
    name = os.path.basename(os.path.normpath(scratch))
    parent = os.path.dirname(os.path.normpath(scratch))
    expected_parent = os.path.normpath(os.path.join(worktree_root, repo))
    if not name.startswith(".integrate-") or parent != expected_parent:
        if os.path.lexists(scratch):
            raise RuntimeError(
                f"refusing to remove {scratch}: not a recognized integrate scratch path")
        return
    if os.path.islink(scratch):
        os.unlink(scratch)
        return
    gitops.git_rc("worktree", "remove", "--force", scratch, cwd=source)
    gitops.git_rc("worktree", "prune", cwd=source)
    if os.path.exists(scratch):
        # A leftover directory that `worktree remove` did not touch (no
        # longer a worktree git recognizes).
        shutil.rmtree(scratch)


def _parse_ls_remote_symref(output: str) -> str:
    """The branch name from `git ls-remote --symref origin HEAD`'s output: the
    line `ref: refs/heads/<branch>\tHEAD` (git's documented shape for a
    symbolic ref query), never `refs/remotes/origin/HEAD` -- nothing is
    written locally."""
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "ref:" and parts[-1] == "HEAD":
            return parts[1].removeprefix("refs/heads/")
    raise gitops.GitError(f"could not parse ls-remote --symref output: {output!r}")


def resolve_base(source: str, want: str | None = None) -> tuple[str, str]:
    """The repo's current base branch and its sha.

    With `want` given (a run's `pr_base`), the base branch IS `want`: with an
    `origin` remote the base ref is `origin/<want>` after a fetch, without one
    it is the local branch `want`. Without `want`, the base is origin's
    default branch when the source has an `origin` remote (a read of the
    remote's HEAD symref, `git ls-remote --symref`, when nothing local names
    it -- never a write to the source), otherwise the source's own
    checked-out branch. Raises gitops.GitError on a failing git call, ref
    included; the caller decides what that means (parallel step 4, unit U2:
    `aos interactions` shares this resolution with `aos integrate` -- neither
    copies it). C1: an empty `want` (a stale '' pr_base sentinel) is treated
    like an absent one, not as a real branch named ''."""
    has_origin = _has_origin(source)
    if want:
        if has_origin:
            gitops.git("fetch", "origin", cwd=source)
            base_branch, base_ref = want, f"origin/{want}"
        else:
            base_branch, base_ref = want, want
        base_sha = gitops.git("rev-parse", base_ref, cwd=source).stdout.strip()
        return base_branch, base_sha
    if has_origin:
        gitops.git("fetch", "origin", cwd=source)
        try:
            symbolic = gitops.git("symbolic-ref", "--short", "refs/remotes/origin/HEAD",
                                   cwd=source).stdout.strip()
            base_branch = symbolic.removeprefix("origin/")
        except gitops.GitError:
            ls_output = gitops.git("ls-remote", "--symref", "origin", "HEAD", cwd=source).stdout
            base_branch = _parse_ls_remote_symref(ls_output)
        base_ref = f"origin/{base_branch}"
    else:
        base_branch = gitops.git("rev-parse", "--abbrev-ref", "HEAD", cwd=source).stdout.strip()
        base_ref = base_branch
    base_sha = gitops.git("rev-parse", base_ref, cwd=source).stdout.strip()
    return base_branch, base_sha


def integrate_task(client: httpx.Client, *, task_id: str, keep: bool, environ: dict[str, str],
                   out: IO[str], err: IO[str], backend: str) -> int:
    if backend != "openshell":
        print(f"unsupported backend {backend!r}; integration requires openshell", file=err)
        return 2
    # -- step 1: the task and its newest review run --------------------------
    response = client.get(f"/tasks/{task_id}")
    if response.status_code >= 400:
        print(f"task {task_id}: HTTP {response.status_code}", file=err)
        return 1
    task = response.json()
    if task.get("status") != "review":
        print(f"task {task_id} is not review ({task.get('status')})", file=err)
        return 1
    run = next((r for r in task.get("runs", []) if r.get("status") == "review"), None)
    if run is None:
        print(f"task {task_id} has no run in review", file=err)
        return 1
    head = run.get("pr_head_sha") or run.get("result_commit")
    if not head:
        print(f"task {task_id}'s review run has no commit", file=err)
        return 1

    env = harness.env_from(environ)
    source = os.path.join(env.repo_root, task["repo"])
    worker = socket.gethostname()

    def post(*, base_branch: str, base_sha: str, status: str, output: str) -> int:
        post_response = client.post(f"/tasks/{task_id}/integration", json={
            "run_id": run["id"], "head_sha": head, "base_branch": base_branch, "base_sha": base_sha,
            "status": status, "output": output, "worker": worker,
        })
        if post_response.status_code >= 400:
            print(f"posting the integration check failed: HTTP {post_response.status_code}", file=err)
            return 1
        if base_branch:
            print(f"integration {status} {task_id} {head[:8]} onto {base_branch} {base_sha[:8]}",
                 file=out)
        else:
            # A5: a check that never had a base (the source is missing, not a
            # git repository, or resolving the base itself failed) prints its
            # output's first line instead of the "onto <base>" line.
            first_line = output.splitlines()[0] if output else ""
            print(f"integration error {task_id} {head[:8]}: {first_line}", file=out)
        return 0 if status == "passed" else 1

    try:
        commands = list(dict.fromkeys(workflow_file.run_acceptance(run)))
    except workflow_file.WorkflowError as exc:
        return post(base_branch="", base_sha="", status="error", output=str(exc))

    try:
        settings = openshell_runner.settings_from(environ)
    except openshell_runner.AdapterError as exc:
        return post(base_branch="", base_sha="", status="error", output=str(exc))

    # -- step A5: the source repository must exist and be a git repository ----
    if not os.path.isdir(source):
        return post(base_branch="", base_sha="", status="error",
                    output=f"source repository {source} does not exist")
    # C6: `git rev-parse --git-dir` walks up to a parent repository's .git,
    # so a plain directory nested inside another repository passed this
    # check. The source is a repository only when its own top level -- real
    # paths compared -- is the source itself.
    toplevel_check = gitops.git_rc("rev-parse", "--show-toplevel", cwd=source)
    if (toplevel_check.returncode != 0
            or os.path.realpath(toplevel_check.stdout.strip()) != os.path.realpath(source)):
        return post(base_branch="", base_sha="", status="error",
                    output=f"{source} is not a git repository")

    # -- step 3: the base branch and its sha ----------------------------------
    base_branch = ""
    base_sha = ""
    try:
        base_branch, base_sha = resolve_base(source, want=run.get("pr_base"))
    except gitops.GitError as exc:
        return post(base_branch=base_branch, base_sha=base_sha, status="error", output=exc.stderr)

    # Read only the authoritative default's v2 policy, never the merged tree.
    try:
        default = policy.resolve_default(source)
        policy_v2 = openshell_runner.read_policy_v2(source, default)
        repo_workflow = workflow_file.read_workflow(source, default)
        if (repo_workflow is not None and repo_workflow.test_command
                and repo_workflow.test_command not in commands):
            commands.append(repo_workflow.test_command)
        from agent_os.credential_bootstrap import require_clean_initial_environment
        require_clean_initial_environment()
    except (policy.PolicyError, openshell_runner.AdapterError, workflow_file.WorkflowError, ValueError, OSError) as exc:
        return post(base_branch=base_branch, base_sha=base_sha, status="error", output=str(exc))

    # Plan services before creating a worktree, using sandbox-facing addresses.
    try:
        pg_plan = openshell_services.plan(source, settings.platform, environ)
    except ValueError as exc:
        return post(base_branch=base_branch, base_sha=base_sha, status="error", output=str(exc))

    # -- step 4: head must be a commit already in the source ------------------
    verify = gitops.git_rc("cat-file", "-e", f"{head}^{{commit}}", cwd=source)
    if verify.returncode != 0:
        return post(base_branch=base_branch, base_sha=base_sha, status="error",
                    output=f"commit {head} is not in {source}")

    # Refuse an unavailable gateway; never fall back to unconfined execution.
    if not _gateway_reachable():
        return post(base_branch=base_branch, base_sha=base_sha, status="error", output=GATEWAY_UNREACHABLE)
    openshell_runner.reconcile_quietly(client, settings, err)

    attempt: openshell_integrate.Attempt | None = None

    scratch: str | None = None
    previous_sigterm = None
    owns_sigterm = threading.current_thread() is threading.main_thread()

    def terminate_integration(signum: int, frame: Any) -> None:
        # A second TERM must not interrupt teardown halfway through.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt("integrate received SIGTERM")

    if owns_sigterm:
        previous_sigterm = signal.signal(signal.SIGTERM, terminate_integration)
    try:
        # Reserve a private path atomically: another attempt (or --keep) may
        # still own any existing scratch worktree for this task.
        scratch_parent = os.path.join(env.worktree_root, task["repo"])
        os.makedirs(scratch_parent, exist_ok=True)
        scratch = tempfile.mkdtemp(prefix=f".integrate-{task_id}-", dir=scratch_parent)
        try:
            gitops.git("worktree", "add", "--detach", scratch, base_sha, cwd=source)
        except gitops.GitError as exc:
            return post(base_branch=base_branch, base_sha=base_sha, status="error", output=exc.stderr)
        # -- step 6: the merge -------------------------------------------------
        # Through policy.GIT like every other runner-side git call: no repository hook (pre-merge-commit,
        # post-merge, post-checkout) runs in the scratch worktree.
        merge_result = gitops.git_rc("-c", "user.name=aos", "-c", "user.email=aos@localhost",
                                      "merge", "--no-ff", "--no-edit", head, cwd=scratch)
        if merge_result.returncode != 0:
            conflicts = gitops.git_rc("diff", "--name-only", "--diff-filter=U", cwd=scratch).stdout
            merge_output = (merge_result.stdout or "") + (merge_result.stderr or "")
            gitops.git_rc("merge", "--abort", cwd=scratch)
            return post(base_branch=base_branch, base_sha=base_sha, status="conflict",
                       output=conflicts + merge_output)

        # -- step 7: the combined check ----------------------------------------

        merge_sha = gitops.git_rc("rev-parse", "HEAD", cwd=scratch).stdout.strip()
        attempt = openshell_integrate.Attempt(settings, pg_plan, policy_v2, task_id=task_id, run_id=run["id"],
                                              environ=dict(environ))
        status, acceptance_output = attempt.check(scratch, merge_sha, commands)
        if owns_sigterm:
            # The verdict is in hand: from here through the post and the cleanup a TERM is ignored.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        return post(base_branch=base_branch, base_sha=base_sha, status=status, output=acceptance_output)
    finally:
        try:
            if attempt is not None:
                # After the post: the cleanup record, never a change to the verdict.
                lines = attempt.cleanup(err)
                if lines:
                    print("integration cleanup:", file=out)
                for line in lines:
                    print(line, file=out)
            # -- step 10: the scratch worktree is removed unless kept ---------
            if scratch is not None:
                if keep:
                    print(f"scratch worktree kept at {scratch}", file=out)
                else:
                    _remove_scratch(source, scratch, env.worktree_root, task["repo"])
        finally:
            if owns_sigterm:
                signal.signal(signal.SIGTERM, previous_sigterm)
