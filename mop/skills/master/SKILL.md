---
name: master
description: Run the master loop on a mop pool — hold the bug queue, triage one ticket at a time with the operator (essence + proposed fix, they approve or correct), dispatch the approved fix to a free puppet in its own clone, land the fixes one at a time (by the landing token where the project keeps one), and keep the integration branch's pipeline healthy. Use when the operator asks to "run /master", start triage, dispatch tickets to puppets, act as master, or manage a pool of Claude sessions working a tracker.
---

# Master: the pool control loop

You hold the queue and the operator's attention; puppets hold the keyboards.
Loop: **gather → triage → dispatch → landing → next**.

Five invariants carry everything below; the sections are their mechanics:

1. **Nothing is dispatched without approval** — one ticket at a time,
   reasoned aloud with the operator.
2. **The pool is the `agents` roster.** A puppet is a Nomad job created by
   the `puppet` tool (`action: add`); no other claude instance, however
   plausible its origin, is a puppet and none of them gets work.
3. **One clone = one ticket**, and every dispatch carries its full context —
   a puppet can be reborn blank at any moment.
4. **One landing at a time**: exactly one puppet between merge and push —
   by the landing token, or by git refusing the losing push where the
   project's rules drop the token — and the gate on the integrated result
   is the SMALLEST thing that keeps the integration branch buildable —
   usually just the format/lint check. Tests are CI's job on the push.
5. **Silence is not success**: the puppet's report is the signal; idle notices
   and deadlines are only a safety net.

The skill describes the loop, not any project. The project's own rules file is
older than this skill wherever they disagree, and is read first.

## Step 0 — learn the project

From the project's rules file, stated back to the operator in your first
message so a wrong assumption dies before it reaches a puppet: the
integration branch — run `echo "$MOP_BRANCH"` FIRST: a non-empty value is
the operator's own branch (`git config mop.branch`, #249) and it IS the
integration branch, whatever origin/HEAD, the repository's default branch
or the team's merge history suggest; only when it is empty, the one the
rules name; the tracker and the exact commands to read, comment,
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

## Master shell and the project

The loop runs from a **master shell**: `mop master` in the project's working
copy derives the project from the origin, supplies the project's credentials and
drops you into claude.

Before doing anything, **verify you are in one**: `mop mcp --check` prints
the profile — "master of project <name>", "operator" or "node". Tool presence
proves nothing: on the control machine every session has the mop tools. Not
a master → tell the operator to run `mop master` there and stop. Do not
bypass this with `Agent` subagents or the built-in `SendMessage` — they
cannot see the pool, and the work goes nowhere while looking done.

From the project boundary: the `agents` tool shows **only this project's
puppets**; tools naming a puppet refuse foreign ones. A jump to another
project needs a master shell there; say so, don't try to cross.

**Several masters per project are legal, and the tools keep them apart** —
there is nobody to ask and no side agreement to make. Each master is a
person logged in under their own login, and the pool sees that login on
every request. Your `send` to a puppet makes you the owner of its ticket:
the OWNER column in `agents` shows who leads each puppet. A puppet another
master leads — work in its clone, or dispatched minutes ago — refuses your
`send` with that master's login, and so does every tool that changes a
puppet: `slash`, `restart`, `update`, `recycle`, `wipe`, `delete`. Read the
refusal as the answer, not as an obstacle:

- **Dispatch only to free puppets with no owner or your own.** Another
  master's puppet is not a spare, even when it looks idle: an empty OWNER
  cell is the only invitation.
- **Ownership lapses by itself** once the clone is clean, on its default
  branch, and the dispatch is older than the window (ten minutes); the next
  `send` then takes the puppet without `force`. That is why every report
  ends with the clone switched back to the default branch.
- **`force=true` is taking over someone's work**, not clearing a stale
  flag: an owner whose clone still holds work never lapses. Use it only
  when that master is gone for good or agrees, look at what the clone holds
  first (`tail`, the branch), and tell them — the reply names whom you took
  it from.
- **The landing order is per project** (see Landing): where the project
  keeps a landing token, all its masters share the one token; where it does
  not, git itself decides a race between two landings.

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
  means one of two things. No free slot (`pool` shows node capacity): stop
  growing, tell the operator, work with what you have. Or no image: on a
  node whose puppets live in containers (driver `pve`) a puppet lands only
  where its project's image is baked, and without one it waits forever,
  however many slots are free. The image is the operator's: `mop project
  add --update <origin>` bakes it; `nodes` shows which projects each node
  serves, if your login has the admin role. A brand-new project has no
  project on the bus yet, and the tool cannot refuse early: a fresh puppet
  that comes up but cannot reach the pool is the missing project, not a
  malfunction — the operator's `mop project add <origin>` fixes it in one
  command.
- **The environment is a ticket, not a shell command.** A puppet reporting
  "no postgres here" or "tool X missing" has found a gap in the project's
  environment, and the fix lives in the repository: `.mop/sandbox.yaml` for
  packages and the body's size (baked into the image once, so it needs a
  rebuild), `.mop/bootstrap.yaml` for env files and setup played at every
  start (docs/BOOTSTRAP.md); a missing key or password is the operator's
  `mop secret`. A package the puppet installed by hand is gone on the first
  recycle, and in a container it goes with the body — a report that leans
  on such an install is not done. Such a ticket splits in two layers: the
  environment first, the fix on top of it.
- **Puppets are sticky.** A closed ticket does NOT release a puppet: he
  returns to "free" and waits for the next dispatch. Everything that makes a
  puppet fast — the clone, the warm build tree, a session steeped in the
  project — is exactly what deletion throws away, while an idle puppet costs
  only his memory reservation.
- **Release** (`puppet(action="remove")`; on a host node the clone stays
  and a puppet of the same name reuses it, in a container it goes with the
  body) only when the queue is empty and nothing is expected, or the operator
  ends the run — and only puppets your own record shows free: last report
  received, token returned if the project keeps one. Never delete
  mid-ticket; killing a session loses its unsaved work, and removing a busy
  puppet is the operator's decision, not yours. The node's disk watchdog
  sweeps the leftovers later.
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
| `AGENT SILENT (…)` | no, and do NOT touch the puppet — the node's agent is silent while the puppet may be working fine; a restart kills live work. Cured by the operator with `mop server deploy` |
| `not logged in`, `login expired` | no — the `login` tool, then a nudge (`doctor(fix=true)` does both: hands out credentials and sends «продолжай»). Never a restart: claude picks fresh credentials up on the next turn, and a restart wipes his conversation |
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
`mcp__mop__slash(name, "/model <m>")` picks another model and leaves the
session untouched — reach for it first, it costs nothing; models are served
by the pool's single LLM proxy, so any name the proxy serves is one slash
away. `puppet(action="restart")` never keeps the conversation — that is
deliberate, since returning a stuck puppet to the context he stuck on would
just reproduce the jam. If the proxy itself is down or out of quota, that is
the operator's to fix (the panel, `mop doctor`) — no puppet-side move helps.

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
(A project without the token: push your branch, report, and wait for my
"land"; a rejected push is fetch, merge again, rerun, push — never force.)
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

**Whether there is a token is the project's call — read its rules file.**
The token buys one thing: exactly one puppet between merge and push, so
that two long gates do not race and the second push does not bounce
non-fast-forward after its gate ran against an integration that no longer
exists. It pays for itself when the gate on the merged tree runs for tens
of minutes.

**A project whose rules drop the token** (a gate of seconds, where git's
own refusal of the losing push is the lock) lands without one. You still
approve each landing — accept the report, and name the order between
tickets when one must land before another ("land only once #N is in the
integration branch", checked with `git merge-base --is-ancestor`) — but
nothing is taken or given back. The puppet fetches, merges, runs the gate,
pushes once; a rejected push means fetch, merge again, rerun the gate, push
again — never a force push.

**A project that keeps the token**: grant it to exactly one puppet, take it
back on the report — through the `landing` tool (`mop landing`), not in
your head: `landing take <puppet>` before you tell the puppet it may land,
`landing give` on its report. The token lives with the cluster service,
one per project, so every master of the project sees the same one: `take`
refuses while another master holds it, naming who, for which puppet and
since when — wait for their `give`, or agree with them. The order of your
own queue is still yours. `--force` only for a holder that is gone for
good; it names whom it took the token from — tell them.

In order (under the token, where the project keeps one):

1. Fresh integration branch, `git merge --no-ff` — one merge commit per
   ticket keeps `git revert -m 1 <merge>` a one-push rollback.
2. **The format/lint check on the integrated result, and nothing more
   unless you can name what it buys.** Tests are CI's job on the push.
   Running them under the token serialises tens of minutes behind every
   landing while the queue stands still, and consecutive pushes cancel each
   other's runs anyway. A project whose rules file says otherwise wins;
   check it before dispatching.

   **Find out what the format check already does before adding to it.**
   A lint step that type-checks (`cargo clippy --workspace --all-targets`,
   `tsc --noEmit`, `mypy`) IS the compile, and stricter — a separate build
   phase beside it buys nothing and costs the whole build twice. Measured
   here: a gate ran `rug fmt --check` AND `cargo check --workspace
   --all-targets` for a whole session before anyone read `bin/fmt` and saw
   clippy already in it.

   **The one thing worth gating on is that the integration branch still
   BUILDS**, because everyone rebases on it: a head that does not compile
   stops the next puppet with an error they cannot attribute — they cannot
   tell a broken branch from a broken base. Everything past that is
   post-hoc, and post-hoc is fine: CI runs on the push, and you fix forward.

   **Full per-crate suites under the token are the classic waste.** They are
   green at the author, green after the merge, and they cost the queue tens
   of minutes per package. If you are tempted, first ask what a landing gate
   caught this week that CI would not have caught twenty minutes later.
3. **One** push (a short-lived ref of the branch, if the integration branch
   is checked out somewhere else).
4. Return the token if there is one, report, wait for the next task. The
   puppet does not wait for its pipeline — the master owns the verdict on
   the integration branch, and a puppet holding the token until green is
   the same stall.

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
- **Keep the landing on ONE puppet while the queue is long.** The build
  directory is keyed by the clone, so a landing that moves between puppets
  pays a cold compile every time — here that was over half the gate's wall
  clock. Same puppet, warm tree; branches reach it through the origin
  anyway, so nothing else changes.
- **Do not dry-run the merge separately when the gate compiles.** It is the
  same build twice, and the gate's is the better one: it runs on the tree
  that goes out, not on a copy of it.
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
