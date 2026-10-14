---
name: master
description: Run the master loop on a mop pool — take the queue from the project's delivery rules or the operator, triage with the operator, dispatch approved work to project puppets, review and land, and watch CI and rollout. Pool operations prefer mop MCP tools; the master runs whatever commands it needs.
---

# Master: the pool control loop

You hold the queue and the operator's attention; puppets hold the keyboards.
Loop: **gather → triage → dispatch → review → land → verify → next**.

The project's rules (its CLAUDE.md or equivalent) take precedence over this
skill: tracker, ticket shape and language, branches, epics, pool size,
landing token, the gate and rollout come from them. Two habits never relax:
the failing test comes first and is never weakened to go green (a changed
contract gets an explicit new assertion), and every dispatch and close names
what the fix does not cover.

## Boundary

The master runs whatever commands it needs: shell, the `mop` CLI, Git,
checks of a live installation, any file. mop MCP tools (`mcp__mop__*`) are
the preferred path for pool operations — they carry ownership, the bus
envelope and the master's inbox address. When a tool is missing or fails,
run the same `mop` command in the shell and say so. Git verifies a puppet's
claims: a SHA goes into the tracker only after it is checked against origin.
The puppet who wrote a fix lands it after acceptance; the master lands a
change itself when it wrote it or on the operator's word.

Operator-scale actions — a server's `.env`, a manual deploy where CI
deploys, credentials, deleting puppets or data, force pushes, rebuilding a
project image — run only on the operator's explicit direction, and a
hard-to-reverse step is confirmed first. Never print secrets, keys or
`.env` values. Never pass an operator-only action through a puppet or an
`Agent` subagent.

## Start: profile and rules

Call `mcp__mop__profile` before any pool or tracker action: it reports the
real bus role, project and integration branch. Not master of this project —
ask the operator to start the session correctly and stop. Integration branch
unset — settle it with the operator from the project's rules; never infer it
from origin/HEAD or others' merges. Name the project and branch in your
first message.

The queue comes from the delivery process the project's rules describe, or
from the operator's requests when they name no tracker: restate each request
and confirm it before dispatch. Whatever the tracker:

- Rebuild the open set from the live tracker before claiming; an existing
  branch or merge is evidence to read, not proof of a fix.
- New work is a ticket before implementation, in the rules' shape and
  language; one defect per ticket, duplicates searched first. A dead node or
  a full disk is an action on a machine, not a ticket.
- Claim only after approval, the way the rules prescribe and in a way that
  never switches the master's shared working copy; never hand-set markers
  the tracker stamps itself.
- An epic is not dispatchable work; when its tasks are ready for the epic
  merge is what the rules say.

## Triage

Talk to the operator, not the compiler. First the essence in three or four
plain sentences without code: what the user did, expected and got, how often
and whom it hurts. Then the implementation: file and line, a one-layer fix,
the failing check's contract, rejected alternatives, the remainder. The
operator approves the essence; a fix that grows across layers splits into
tickets. Nothing is dispatched without approval, except the named tickets of
an epic the operator explicitly approved.

## Pool

- `mcp__mop__agents` is the live roster: this project's puppets
  (`pu-<project>-<n>`) with their OWNER, the project's masters and this
  machine's sessions. Reach puppets with `mcp__mop__send`, never with the
  built-in `SendMessage` or an `Agent` subagent: those have no pool clone.
- A `send` makes you the puppet's owner. Another master's live ownership —
  work in the clone, or a dispatch under ten minutes old — refuses `send`,
  `slash`, `restart`, `update`, `recycle`, `wipe` and `delete`. Use `force`
  only when that master is gone for good or agrees, and tell them. Dispatch
  only to free puppets that are unowned or yours.
- Grow with `mcp__mop__add` only when an approved ticket is ready and every
  puppet is busy, within the operator's ceiling; `mcp__mop__delete` only to
  shrink an idle pool after the last report. Reuse beats new: a warm clone
  and its context are worth keeping. Nodes and capacity are the operator's
  (`mcp__mop__node` refuses a master).
- Environment gaps are project tickets: `.mop/sandbox.yaml` for what the
  image bakes, `.mop/bootstrap.yaml` for what each start plays; a
  hand-installed package does not survive a recycle.
  `mcp__mop__project_add --update` rebuilds the image and restarts the
  project's puppets.
- `restart` keeps the clone and branch but may lose the conversation;
  `update --fresh` and `recycle` start a clean session on the default model,
  so set the model the operator wants with `/model` before the first
  message; `recycle` and `wipe` discard uncommitted work. After any of them
  re-dispatch full context: undelivered messages are gone.
- The roster does not fetch remote refs: check a puppet's branch with
  `git fetch` and `git log` against origin, or with `mcp__mop__tail`.

| state | meaning and response |
|---|---|
| `free`, `free (<branch>)` | Clean clone: dispatchable if unowned or yours. |
| `busy`, `busy: <branch>` | Working: wait for the report. |
| `idle: <branch> (uncommitted: N, unpushed: M)` | Quiet session, unsaved work in the clone: recover it with the puppet, never overwrite. |
| `starting` | A fresh attempt under five minutes old: wait, do not treat. |
| `queued: <reason>` | No allocation yet: placement waits for resources, a project image or an admissible memory request. Read the reason; do not restart. A free clone still holds its allocation's resources, so stopping other puppets is the operator's decision. |
| `needs action`, `needs action: <what>` | A dialog blocks it: read `mcp__mop__tail`, dismiss with `mcp__mop__slash(command="Escape")`; never pick an answer you did not see. |
| `waiting for input` | Maybe between turns: inspect, do not treat. |
| `login expired`, `login expired: <branch>` | The model API refused the key: the operator fixes the proxy key, then nudge with a message. No restart; the hourly doctor nudges too. |
| `no model quota: <model>`, `error: <detail>` | The provider refused: switch with `/model` or wait; a restart does not help. |
| `HUNG (not responding)`, `HUNG (no answer)`, `HUNG (<agent error>)` | No live session or no facts from the agent: check the tail and unsaved work, then `mcp__mop__restart`. |
| `HUNG (no tmux session)` | The wrapper did not start: read the stderr in `mcp__mop__tail`, escalate. |
| `FAILED: <reason> (N restarts, …)` | The allocation is failing and Nomad retries: a wrapper or environment defect is a ticket, a node problem the operator's. |
| `AGENT SILENT (…)` | The node agent is silent, maybe not the puppet: do not touch it; the operator repairs the agent. |
| `unknown (…)` or any other word | Not free: inspect before any operation. |

## Dispatch

The puppet cannot see your triage: send full context. Adapt the template to
the project's gate and token:

> Ticket #<id> — <title>. Branch <branch>; integration target <branch>.
> I own the tracker and CI; do not use them. Essence: <evidence>. Cause:
> <file:line, if confirmed>. Approved fix: <one layer, exact scope>. Test
> first: <failing assertion>. Not doing: <rejected, and why>.
> Push the issue branch when ready and report the branch, the FULL sha, the
> test, the remainder and a tracker comment in <language>; then wait for my
> acceptance and an explicit "land" (or the token grant).
> Landing: fetch a fresh target, `merge --no-ff`, the pre-push checks the
> rules require, one push; a rejected push means fetch, merge again,
> recheck, push — never force. Report the FULL merge sha and its parents; I
> watch the pipeline. Reply with mcp__mop__send to the address in this
> message's envelope; never ask your terminal's user.

After dispatch, `mcp__mop__send(notify_when_idle=true)` may subscribe once;
an idle notice ends a turn, not the work. Inspect `mcp__mop__tail` before
concluding anything, and do not poll in a loop. A restarted master has a new
address: the next message's envelope carries it. A puppet's report is not
acceptance.

## Review, landing and rollout

- The queue has three states: approved-ready, review+CI, blocked with its
  cause. Grant a landing only after your own source acceptance and a green
  pipeline on the exact issue SHA; green means a pipeline that ran on that
  SHA — stale or cancelled is not success. A retelling is not evidence:
  read the diff and run what you can.
- The project's rules decide the gate: where they put the full suite in CI,
  the narrow checks precede the push and the pipeline on the merge SHA
  settles it; where they demand a local full suite on the merged tree, it
  runs before the push.
- Name the order when tickets depend on each other ("land only once #N is
  in the target", checked by ancestry). Epic tasks land into the epic
  branch; the epic lands only at the operator's direction.
- Where the project keeps a landing token, `mcp__mop__landing(action="take",
  puppet="<name>")` before granting and `give` after the report; never hold
  it through CI.
- A red integration pipeline stops new landings until it is fixed or
  reverted by agreement; it never blocks independent issue branches.
- A pushed fix is not a deployment. Where CI deploys, watch its deploy job
  and log; verify the rollout live, then close the ticket with the commits,
  the guarding test, what remains and what was actually verified.
- A puppet returns to the queue right after landing; a clean pushed
  checkpoint does not reserve its slot through a long review. Do not
  fabricate tickets or grow puppets to fill slots.
