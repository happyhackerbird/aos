# Architecture

[Documentation index](README.md) · [Validation](release-validation.md)

## Components

| Component | Responsibility |
|---|---|
| [API](../agent_os/api.py) and [store](../agent_os/store.py) | Task state, execution snapshots, leases, transitions, request ledger and HTTP routes |
| [CLI](../agent_os/cli.py) | Operator commands and run-scoped agent commands |
| [Runner](../agent_os/openshell_runner.py) | Repo preflight, OpenShell harness execution, heartbeat, acceptance and result reporting |
| [Integration check](../agent_os/integrate.py) | Scratch merge plus frozen run acceptance and current repository-wide tests |
| [Interaction review](../agent_os/interactions.py) | Model-written review of two branches; no automatic fixes |
| [Code lane](../agent_os/lanes/code.py) | PR and merge observation, code review cards |
| [Super Productivity lane](../agent_os/lanes/super_productivity.py) | Read tasks from the app's sync files into the backlog |
| [Telegram](../agent_os/telegram.py) and [health](../agent_os/health.py) | Code review cards, notifications and lane health |
| [Operator channel](../agent_os/operator_channel.py) | The interface for agents' questions to the operator and her answers (`notify`, `match_reply`); Telegram is its one implementation |

The bus is a single-process service backed by SQLite and an append-only activity
log, `runs.jsonl`. The runner executes separately on the host with the repository
and OpenShell CLI installed; the harness itself runs in the workload image.
Optional external services are Telegram, GitHub and Super Productivity (Dropbox).

## Positioning

AOS is an opinionated task orchestration and execution system, not just a record
or a session launcher. It overlaps with Gas City: both coordinate agents against
durable work. AOS's distinguishing contracts are mandatory OpenShell execution
under committed repository policy, frozen runner-executed acceptance, separate
integration checks and operator acceptance/merge. Lease expiry blocks a run;
the dispatcher never automatically requeues failed or blocked tasks.

Remote terminal access is not exclusive to AOS: Gas City also documents a Herdr
backend. AOS's integrated interactive Herdr coordinator and sandboxed child-task
workflow is built and experimental (proven on the server on 2026-10-22); see
[task families](reference/operations.md#task-families).
This comparison distinguishes
observed capabilities from unproven equivalence; it does not establish that Gas
City cannot enforce AOS's contracts.

## Coding task lifecycle

1. The operator supplies a repository, brief, optional acceptance commands, harness
   and host, then authorizes the task (`ready`), manually or through the task board.
2. The dispatcher selects authorized tasks for a worker with capacity. `aos run`
   reads repository workflow and permission files at the same authoritative commit,
   resolves acceptance, and refuses missing or invalid prerequisites before claiming.
   The bus atomically saves the effective checks with the claim and rejects a stale
   task specification. One task has one live run; different tasks may run in the
   same repo up to the configured cap.
3. The runner provisions an isolated job copy in OpenShell and starts the harness.
4. The runner heartbeats while the harness runs. The harness gets a run credential
   for pull-based requests, scope declarations and overlap notices.
5. The runner checks the harness result and reruns task acceptance. Successful
   changes enter review; failures or blocked execution retain a report for the operator.
6. With GitHub configured, the task branch can be pushed and opened as a PR.
   `aos integrate` checks the result merged onto the current base in a scratch
   worktree. Merge requires the matching passing head/base check by default.
   The operator merges; the task is done when GitHub confirms it. Local acceptance
   is available for workflows without a PR.

A requeued task continues from its recorded task branch when available and includes
the previous report in its prompt. New ordinary tasks start at the authoritative
default commit. Family children use their recorded base or an explicitly exported
candidate; imports select exact child revisions rather than sharing a mutable checkout.

## Coordination and authority

Agents declare scopes; the bus records deterministic overlap findings and soft
dependencies. These warnings inform the operator and agents rather than refusing
a claim. Enabled messaging provides explicit tools and automatic delivery;
pull-only mode disables automatic delivery while preserving the tools.
These paths do not wake an already-idle interactive session.
`aos interactions` produces suggestions, never executes its proposed commands or
merges/requeues tasks. The operator remains responsible for dispatch and merge.

## Boundaries and retained state

Both task execution and integration acceptance use OpenShell, not an unconfined
host harness. Repository policy, worker credentials and operator plugins are
separate inputs. Fresh acceptance receives the result tree without the job's
harness authentication or run credential.

The runner checks effective sandbox policy and, when messaging is enabled,
authenticated sandbox-to-bus reachability before starting the harness. A policy
grant is not a guarantee that DNS, routing or the bus works.

Task branches and retained evidence outlive compute. The runner records cleanup
separately from the verdict; reconciliation retries owned sandbox/database cleanup
after a runner loss. Integration scratch-worktree cleanup is separately guarded.
There is no general automatic retirement of source task branches.

The [.aos credential handling and Git guard](reference/cli.md#requests-and-credentials)
protect the run credential lifecycle. They do not erase secrets in Git history or
make untrusted task instructions safe. See [security boundaries](../SECURITY.md)
and the [validation qualifications](release-validation.md).
