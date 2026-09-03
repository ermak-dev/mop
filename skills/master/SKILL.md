---
name: master
description: Run the master loop on a mop pool — hold the bug queue, triage one ticket at a time with the operator (essence + proposed fix, they approve or correct), dispatch the approved fix to a free puppet in its own clone, hand out the landing token so puppets ship one at a time, and keep the integration branch's pipeline healthy. Use when the operator asks to "run /master", start triage, dispatch tickets to puppets, act as master, or manage a pool of Claude sessions working a tracker.
---

# Master: the pool control loop

You hold the queue and the operator's attention; puppets hold the keyboards.
Loop: **gather → triage → dispatch → landing token → next**.

Five invariants carry everything below; the sections are their mechanics:

1. **Nothing is dispatched without approval** — one ticket at a time,
   reasoned aloud with the operator.
2. **The pool is the `agents` roster.** A puppet is a Nomad job created by
   the `puppet` tool (`action: add`); no other claude instance, however
   plausible its origin, is a puppet and none of them gets work.
3. **One clone = one ticket**, and every dispatch carries its full context —
   a puppet can be reborn blank at any moment.
4. **Landing token**: exactly one puppet between merge and push; the
   integrated result gets the format check and the ticket's own tests, and
   the full suite stays CI's job on the push.
5. **Silence is not success**: the puppet's report is the signal; idle notices
   and deadlines are only a safety net.

The skill describes the loop, not any project. The project's own rules file is
older than this skill wherever they disagree, and is read first.

## Step 0 — learn the project

From the project's rules file, stated back to the operator in your first
message so a wrong assumption dies before it reaches a puppet: the
integration branch; the tracker and the exact commands to read, comment,
take and transfer tickets — **all run in YOUR session, never in a puppet's**;
what the landing gate is and what the project leaves to CI; the format
check; the fast narrow test; branch naming;
the ticket body language; how to read CI from the terminal. If the tickets
live only in the operator's head — say so and stop.

Two habits outrank any project's rules, because the trust in the loop stands
on them: **the failing test comes first and is never weakened to go green**
(rewriting a test that encodes a contract you are deliberately changing is
strengthening — say so in the dispatch); and **name what the fix does NOT
cover** — every dispatch lists the rejected alternatives, every close names
the remainder.

## Master shell and the shard

The loop runs from a **master shell**: `mop master` in the project's working
copy derives the shard from the origin, supplies the shard's credentials and
drops you into claude.

Before doing anything, **verify you are in one**: `mop mcp --check` prints
the profile — "master of shard <name>", "operator" or "node". Tool presence
proves nothing: on the control machine every session has the mop tools. Not
a master → tell the operator to run `mop master` there and stop. Do not
bypass this with `Agent` subagents or the built-in `SendMessage` — they
cannot see the pool, and the work goes nowhere while looking done.

From the shard boundary: the `agents` tool shows **only this project's
puppets**; tools naming a puppet refuse foreign ones. Several masters per
project are legal, but **they do not share the landing token** — seeing
another master in the roster, ask who runs the queue before dispatching. A
jump to another project needs a master shell there; say so, don't try to
cross.

**Rights are per-session**: never route through a puppet an action forbidden
in your own session — that launders the operator's decision. Carry it back
to the operator instead.

## The pool

The roster — `mcp__mop__agents` — is rebuilt from live facts on every call
and is both membership and the whole picture: nothing depends on your
memory, and a restarted master recovers everything from this one output.
Puppets are addressed by job name (`pu-<project>-<n>`); delivery goes over
the bus via the agent on the puppet's node. The built-in
`SendMessage`/`ListAgents` see only this host's sessions and are removed on
puppets outright — use `mcp__mop__send`.

**Never dispatch to an `Agent`-tool subagent**: ephemeral agents die with
your session, own no clone, are not pool members — a task sent outside the
pool breaks every invariant at once (no isolation, no reusable clone, no
token queue).

An empty roster does not mean "no agents available" — it means none have
been created. Pool size is your job; the operator may set a ceiling:

- **Grow** when an approved ticket is ready and every puppet is busy:
  `puppet(action="add", origin=…)` — the origin is explicit, take it from
  your own working copy — one puppet per simultaneously dispatchable ticket,
  never more. A new puppet takes minutes, not seconds, then appears in the
  roster and takes its first dispatch; an add sitting `pending` for minutes
  means no free slot (`pool` shows node capacity): stop growing, tell the
  operator, work with what you have. A brand-new project has no shard on
  the bus yet, and the tool cannot refuse early: a fresh puppet that comes
  up but cannot reach the pool is the missing shard, not a malfunction —
  the operator's `mop deploy <origin>` fixes it in one command.
- **Puppets are sticky.** A closed ticket does NOT release a puppet: he
  returns to "free" and waits for the next dispatch. Everything that makes a
  puppet fast — the clone, the warm build tree, a session steeped in the
  project — is exactly what deletion throws away, while an idle puppet costs
  only his memory reservation.
- **Release** (`puppet(action="remove")` — the clone stays on the node)
  only when the queue is empty and nothing is expected, or the operator
  ends the run — and only puppets your own record shows free: last report
  received, token returned. Never delete mid-ticket; killing a session
  loses its unsaved work, and removing a busy puppet is the operator's
  decision, not yours. The node's disk watchdog sweeps the leftovers later.
- **Never "fix" a puppet by deletion.** Puppets self-heal, sessions are
  mortal: a dead claude restarts in place, a dead node makes Nomad move the
  puppet and reclone — either way it is a NEW session, empty context,
  undelivered messages lost; the name survives, the memory does not, so the
  cure is a fresh dispatch with full context. A stuck (not dead) puppet is
  treated in place: `doctor(fix=true)` or `puppet(action="restart")` — clone
  and branch survive both. Deletion is only for shrinking.

### Roster states

| state | dispatch? |
|---|---|
| `free`, `free (<branch>)` | yes — clean, everything on a remote; the branch is informational |
| `busy: <branch>` | no — the session is really working |
| `idle: <branch> (uncommitted: N, unpushed: M)` | NO — the session is NOT working, but this work exists nowhere else. Agent died mid-ticket: recover, or ask the operator — never dispatch over it. `busy` and `idle` are different reports: the first you wait out, the second you rescue |
| `needs action`, `needs action: <what>` | no — stuck on a prompt with nobody to answer it. `mcp__mop__tail` first: a dialog → `mcp__mop__slash(<name>, "Escape")`; otherwise `doctor(fix=true)` restarts it. `needs action: resume prompt` means he was asked how to restore a long conversation — Escape cancels the restore, a restart brings him up clean |
| `waiting for input` | no — may just be between turns; deliberately not auto-treated, the operator decides |
| `HUNG (not responding)` | no — `puppet(action="restart")` |
| `HUNG (no tmux session)` | no — the wrapper never reached a working state; `mcp__mop__tail` and `doctor()` |
| `AGENT SILENT (…)` | no, and do NOT touch the puppet — the node's agent is silent while the puppet may be working fine; a restart kills live work. Cured by the operator with `mop deploy` |
| `not logged in`, `login expired` | no — the `login` tool, then a restart (`doctor(fix=true)` does both) |
| `no model quota: <model>` | no — a restart will NOT help: switch the model (`mcp__mop__slash(name, "/model <m>")`) or top up |
| `error: <provider message>` | no — the provider refused the turn; the message carries the reason and, for a quota, when it resets. A restart will NOT help: switch the model or wait it out |
| `unknown (no clone data)` | no — the node did not report the clone, so nothing is known about the work in it. Never a synonym for free: look yourself (`mcp__mop__tail`, or ask the puppet) before doing anything to that slot |

"Uncommitted" and "unpushed" are different numbers shown separately on
purpose — name the right one when re-dispatching. A quota-exhausted puppet
looks perfectly healthy: online, answering, accepting messages — and every
turn dies on a credit error. Remote-tracking refs in clones may be stale
(the roster deliberately does no fetch — that would be a network round per
puppet per call): a surprising puppet gets `mcp__mop__tail` or `mop attach`,
not guesswork.

A quota wall has two exits, and they are not interchangeable.
`mcp__mop__slash(name, "/model <m>")` picks another model **from the same
provider** and leaves the session untouched — reach for it first, it costs
nothing. `puppet(action="update", name, llm=<profile>)` moves the puppet to
a **different provider** and restarts him, but he comes back on the same
conversation, so a ticket in progress survives the move. Mid-ticket that is
the whole point; add `fresh=true` only when the old context is what you want
gone. `puppet(action="restart")` never keeps the conversation — that is
deliberate, since returning a stuck puppet to the context he stuck on would
just reproduce the jam.

## Clone discipline

- **One clone = one branch = one ticket.** Each puppet owns his clone in
  `~/puppets/<name>` on his node; two puppets never share a tree.
- **Build in the default build directory.** An invented path is keyed by the
  agent while the build tree is keyed by the clone — a moved puppet splits
  the cache into a second multi-gigabyte one. A cache shared between clones
  is worse: stale artifacts read as genuine test failures.
- **Branch names carry state**: creating the working branch IS the claim on
  the ticket — do it before touching anything.

## Triage

The triage is addressed to the **operator**, not the compiler, and is read
once, on the move. So it goes in **strictly two passes, and the first one
contains no code**.

**First, the essence in plain language** — three or four sentences from
which "fix it or not" is decidable: what is broken **from the user's point
of view** — what he did, what he got instead of the expected; how often and
whom it hurts — a number with a denominator, money, churn; what is actually
happening, in one phrase, without function names.

**Only then, the implementation**, visibly second: files and lines, the
fix's shape, the failing test's contract, the rejected alternatives, the
remainder.

The operator approves the **essence**, not diffs: he decides whether the
pain is worth a puppet's turn, and for that he needs neither signatures nor
line numbers. The check: **cover everything below the first paragraph with
your hand — if the user's loss is not clear from what remains, the triage
gets rewritten.** Technical detail is not trimmed: it moves whole into the
dispatch, where the puppet reads it.

## Dispatch

The task carries the whole reasoning: the puppet has no access to the triage
conversation and may be reborn without memory.

```
Ticket #<id> — <one-line title>.

Directory: <clone>. Branch: <working branch, already named for you>.
The tracker is mine: I took the ticket and will close it on your report.
Never run tracker commands — you may have no route to it at all.
Essence: <symptom and evidence — the ticket's numbers, not its title reworded>.
Layer that stayed silent: <file:line>.
Approved fix: <what exactly changes, one layer>.
Test first: <what the failing test must assert>.
Not doing: <what the triage explicitly rejected, and why>.

Protocol: failing test → minimal fix → <narrow test> → <format/lint>.
Then ASK ME for the landing token and wait — no merge, no push without it.
With it: merge --no-ff onto a fresh <integration branch> → <format/lint> plus
THIS TICKET'S OWN TESTS on the integrated result → one push → switch back to
<integration branch> (a clone back on the default branch is what reads as
free) → return the token and report. Do not run the full suite and do not
wait for builds: the suite is CI's job on the push, and the token is held
only between merge and push.
Report: branch, FULL sha of the fix commit, FULL sha of the merge, the
guarding test's name, the remainder left, and the tracker comment ready to
paste in <tracker language> — I paste it, not you.
Stop and ask me if a decision changes the fix's shape. If the work does NOT
fix the ticket, say so in the report outright — never let it read as done.

Write me via mcp__mop__send(to="<address from this message's envelope,
from-name>") — reports and questions alike. A session name is not an
address; lost the envelope — take it from the masters table in
mcp__mop__agents. Never open a dialog and never ask your "user": there is no
human at your terminal, and a dialog blocks you from ever reading my reply.
Same for permission prompts: shape the command so none is needed (absolute
paths, one operation per command, no compound cd-plus-write); if it still
hits a guard, send me the command instead of waiting at the prompt.
```

The report arrives over the same channel: the puppet answers into your inbox
on the bus, taking the address from the envelope — don't spell it out in the
dispatch. If a puppet reports his `send` as NOT DELIVERED, it is the address,
not the channel: restarting your session changes your address, and the old
one dies with the old MCP server. Send the puppet any message again — the
fresh address rides its envelope.

## After the dispatch

- `mcp__mop__send(..., notify_when_idle=true)` — once, at the moment of
  token handoff, not later. The node's agent holds the subscription next to
  the puppet's socket and pushes a notice into your session. But it fires at
  the end of a *turn*, not on completion of work — it catches only a dead
  session. **The signal is the report.** Don't resubscribe on every notice,
  don't poll the roster in a loop, don't ask "done yet?".
- A puppet silent past the deadline: **read his screen before concluding
  anything** — `mcp__mop__tail` is the only place a modal dialog is visible;
  the roster cannot tell "working" from "waiting for a human", and your
  messages pile up unread behind the dialog. The cure is to **dismiss** it,
  never to answer it: `mcp__mop__slash(<name>, "Escape")` — your reply is
  already sitting in his queue, and answering a multi-question dialog means
  choosing from a list you cannot fully see. If dialogs recur, that is a
  pool tooling defect, not a fact of life — say so.

## Landing

The puppet who wrote the fix ships it, not waiting for the pipeline:
acceptance is the ticket's own tests, already satisfied at merge.

**The token.** Exactly one puppet between merge and push: two parallel gates
race each other, and the second push bounces non-fast-forward after its
gate ran against an integration that no longer exists. Grant the token to
exactly one, take it back on the report. The queue is yours — no lock file,
no extra tooling.

Under the token, in order:

1. Fresh integration branch, `git merge --no-ff` — one merge commit per
   ticket keeps `git revert -m 1 <merge>` a one-push rollback.
2. **The format/lint check plus the ticket's own tests on the integrated
   result** — what the merge could have broken, not everything the project
   has. The full suite is CI's job on the push: running it under the token
   serialises tens of minutes behind every landing while the queue stands
   still, and consecutive pushes cancel each other's runs anyway. A project
   whose rules file says otherwise wins; check it before dispatching.
3. **One** push (a short-lived ref of the branch, if the integration branch
   is checked out somewhere else).
4. Return the token, report, wait for the next task. The puppet does not
   wait for its pipeline — the master owns the verdict on the integration
   branch, and a puppet holding the token until green is the same stall.

Gate discipline, for any runner:

- Check that **no run is already going** before starting one: a killed
  wrapper does not kill the run beneath it, and two suites grinding against
  one build directory manufacture unreproducible failures.
- Start the run **detached, redirected to its own log file**: a plain
  background job dies with the session's other tasks, and piping through
  `tail` buffers until EOF, hiding progress and exit status alike.
- Suite and lint **in sequence**: started together they queue behind the
  build tool's lock, and a gate waiting on a lock is indistinguishable from
  a hung one.
- The run **writes its own verdict into its log**
  (`{ ./cmd; echo "===EXIT=$?==="; } > gate.log 2>&1`). An exit code
  recovered from outside the run is a reconstruction: a pipe reports its
  last stage (0 over a failed suite), and a background task's status is the
  wrapper's whenever anything is chained after it.
- Watch for the **absence of any process of the run AND an anchored summary
  line** in the log — one pid is not enough (a wrapper exits early), and a
  bare `FAILED` matches the NAME of a passing test. Anchor the pattern.
  Processes gone with no summary = the run was **killed** — a third
  outcome, loud, never to be read as green.
- An early-aborted run can still print a green tail (a failure under
  `set -e` skips the later phases): confirm each phase **by its own
  marker**, not by the absence of complaints.
- A gate blocked on a lock is not hung: identify the holder — and confirm
  the lock is your own run's — before concluding anything.

**Pipeline health is on you.** Consecutive pushes cancelling each other's
runs are by design — never stretch pushes apart. You watch for the other
thing: the integration HEAD must end in a pipeline that **actually ran**; a
failure can look exactly like the last cancelled run with nothing after it.
Know how the project reruns a cancelled pipeline, and expect a shared runner
to queue a batch of landings.

You close or transfer the ticket on the puppet's report, citing the fix
commit, the merge, the guarding test and the remainder — in the project's
language. Pull your own clone to the pushed HEAD first wherever the
transfer command reads local HEAD, or you stamp a stale pipeline into the
ticket.

## Before triage: prove the work is not done

The tracker moves under you: rebuild ticket sets from live output at
dispatch time, never reuse a snapshot. An open ticket does not prove nobody
did the work — a fix lands before anyone gets around to closing:

```bash
git fetch origin -q
git branch -a --list "*/<id>" --list "*/<id>-*"    # anchored: *88* also matches 188
git log <integration-branch> --oneline --grep="#<id>\b" -E
```

A branch means someone is on it; a merge means only the closing is missing.
Read the commit before closing: instrumentation that changes no behavior is
not a fix, a commit declaring its own incompleteness is not a fix, and a
ticket mentioned in a commit ("related to", "left as #N") is usually not its
subject — only the merge of the branch that implements it counts.

## Two traps with nowhere else to live

- **An epic is not dispatchable work**: it names its parts as separate
  tickets and carries explicit triggers. Check that the parts are closed and
  the trigger fired; handing an epic to a puppet asks him to invent the scope
  it was yours to bring.
- **Your own proposal creeps into a composite fix** while you refine it:
  each addition is reasonable on its own, the sum touches several layers —
  and bringing that sum is the master's job, done deliberately. Name one
  layer; list what you dropped.
