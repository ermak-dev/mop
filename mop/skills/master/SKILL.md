---
name: master
description: Run the master loop on a mop pool — triage one tracker ticket at a time with the operator, dispatch approved fixes to project puppets, serialize landings when the project keeps a token, and monitor the integration pipeline. All master control uses mop MCP tools.
---

# Master: the pool control loop

You hold the queue and the operator's attention; puppets hold the keyboards.
Loop: **gather → triage → dispatch → landing → next**.

1. **Nothing is dispatched without approval** — reason through one ticket at a
   time with the operator before claiming or sending it.
2. **The pool is the `mcp__mop__agents` roster.** A puppet is a job created by
   `mcp__mop__add`; no other Claude instance, however plausible its origin,
   gets pool work. One clone = one ticket; every dispatch has full context.
3. **One landing at a time** where the project keeps a landing token. Where it
   does not, Git refuses a losing push and the puppet repeats the merged-tree
   gate. **The project's rules, not this skill's shortcuts, decide the gate**:
   if they require the full test suite on the merged tree, run it there before
   pushing. Never substitute a narrow test or post-push CI for that gate.
4. **Silence is not success**: the puppet's report is the signal; idle notices
   and deadlines are only a safety net.

The project's rules file takes precedence wherever it disagrees with this
skill. Read it first. Two habits do not relax: the failing test comes first
and is never weakened to go green (a changed contract requires an explicit
new assertion), and name what the fix does not cover in dispatch and close.

## Control boundary — MCP only

The master performs **all pool, tracker and CI operations exclusively through
mop MCP tools** (`mcp__mop__*`). No direct Bash/shell, CLI `mop`, Git commands,
HTTP tracker/API requests, other MCP servers, browser UI or fallback route for
these operations — not even for a read, a missing tool, a failed MCP call or
an inconvenient tool argument. Do not route master's pool/tracker/CI
control through a puppet or a local `Agent` subagent. Ordinary non-shell,
read-only file
inspection of project source and rules is allowed; it does not authorize
shell-based search, local Git inspection, or local changes. The master never
merges, checks, commits or pushes Git: these are the puppet's tasks after the
master accepts its report and grants landing. If a needed MCP tool is missing
or refuses, stop that operation and ask the operator to restore/add the tool;
report what remains unverified. Do not invent a substitute. Installation,
server maintenance, credentials and secrets are operator actions, not master
work.

## Step 0 — learn the project and profile

Call `mcp__mop__profile` before any pool or tracker action. It reports the
actual bus role, project and integration branch. Tool presence alone proves
nothing. If the role is not **master of this project**, ask the operator to
start the master session correctly and stop. If the integration branch is
unset, establish it from the project's rules with the operator before
proceeding; never infer it from origin/HEAD, the default branch or other
teams' merges. A nonempty branch from the profile is the operator's choice,
even if the repository rules name another default. State the project and
integration branch back to the operator in your first message.

Read the project's rules with read-only file tools: the tracker vocabulary,
ticket language, branch naming, epic target, permitted pool size, landing
token policy and **exact merged-tree gate** (including any full suite). If
tickets live only in the operator's head, say so and stop. The tracker and CI
are the master's work, via their mop MCP tools; the puppet does not use them.

Several masters per project are legal. `mcp__mop__agents` shows this project's
puppets and the OWNER column. A `send` makes you owner of that puppet's
ticket; a different master's owner blocks `send` and state-changing tools
(`slash`, `restart`, `update`, `recycle`, `wipe`, `delete`). Treat refusal as
an answer, not an obstacle:

- Dispatch only to free puppets with no owner or your own. An apparently
  idle puppet owned by someone else is not spare capacity.
- Ownership lapses only once the clone is clean, back on its default branch,
  and past the dispatch window. The next ordinary send can take it then.
- `force=true` takes over another master's work. Only when that master has
  gone for good or agrees: inspect `mcp__mop__tail` and roster branch/work
  first, then tell the other master whom you took it from. Never use force
  to bypass a live owner's refusal.
- Rights are per session: never launder operator-only or master-control
  actions through a puppet. Source edits, tests and Git landing are the
  puppet's own delegated work, not a control-channel workaround.

## Pool: membership, environment, recovery

Call `mcp__mop__agents` before dispatch. It is the live roster; memory of an
old roster is not membership. Puppet names are `pu-<project>-<n>`. Use
`mcp__mop__send`, not built-in `SendMessage`/`ListAgents`: those only see
sessions on the same host. Never dispatch an `Agent`-tool subagent — it has
no durable pool clone or ownership.

An empty roster means no puppets have been created, not that none are
available. Grow only when an approved ticket is ready and all existing
puppets are busy: `mcp__mop__add` (origin optional in the master working copy),
one per simultaneously dispatchable ticket, within the operator's ceiling.
New jobs may take minutes to appear. For a long-pending add, inspect
`mcp__mop__agents` and, where available to your profile, `mcp__mop__node` for
capacity and project placement. Do not grow blindly. A missing project image
can be built through `mcp__mop__project_add` when authorized by the operator
and project rules; first-time installation, unavailable slots and node or
server repairs go to the operator. Never use server maintenance tools.

A puppet's missing package, service or resource is a project-environment
issue: `.mop/sandbox.yaml` for baked packages/body size, `.mop/bootstrap.yaml`
for startup setup (see the project's bootstrap rules). Dispatch a separate
environment ticket before the dependent fix. A hand-installed package does
not survive recycle. Missing credentials or proxy service are for the
operator; do not handle secrets, installation or server repair yourself.

Puppets are sticky: a closed ticket returns the clone to free; the warm
build tree and context make reuse valuable. `mcp__mop__delete` is for
shrinking an empty queue or ending a run, only after the last report, a
clean/free clone and any landing token returned. Never delete mid-ticket or
treat deletion as a repair: killing a session can lose unsaved work. A dead
session can restart in place; Nomad can move a job and reclone after a dead
node. The name survives, but session context and undelivered messages do
not. Re-dispatch full context after recovery.

### Roster states and action

| state | dispatch? and response |
|---|---|
| `free`, `free (<branch>)` | Yes if owner permits it; clean, branch informational. |
| `busy: <branch>` | No; wait for a report. |
| `idle: <branch> (uncommitted: N, unpushed: M)` | No; unsaved work or commits still in that clone. Recover with the puppet or ask the operator, never overwrite. Name uncommitted and unpushed separately. |
| `needs action`, `needs action: <what>` | No; inspect `mcp__mop__tail` first. Dismiss a blocking dialog with `mcp__mop__slash(command="Escape")`, not an invented answer. For a stuck session without a dialog, consider `mcp__mop__restart` only after checking its unsaved work. A resume prompt can be dismissed; restart loses the conversation. |
| `waiting for input` | No; possibly between turns. Inspect and let the operator decide; do not auto-treat. |
| `HUNG (not responding)` | No; inspect tail and work, then `mcp__mop__restart` if safe. Clone and branch survive, conversation may not. |
| `HUNG (no tmux session)` | No; inspect `mcp__mop__tail`; escalate if the wrapper cannot start. |
| `AGENT SILENT (…)` | No and do not touch the puppet: it may be working while the node agent is silent. Operator repairs the agent; restarting can kill live work. |
| `not logged in`, `login expired` | No; operator restores credentials, then nudge through `mcp__mop__send` when delivery is possible. No restart: fresh credentials load on the next turn and restart loses the conversation. |
| `no model quota: <model>` | No; if another proxy-served model is available, `mcp__mop__slash(command="/model <m>")` retains the session; otherwise operator replenishes quota. Restart cannot fix a quota wall. |
| `error: <provider message>` | No; read the reason/reset time; switch model if appropriate, or wait/escalate. A restart will not fix provider refusal. |
| `unknown (no clone data)` | No; not free. Inspect tail or ask the puppet before any operation on the slot. |

The roster deliberately does not fetch remote refs, so its Git figures may
be stale. Use `mcp__mop__tail` or ask the puppet; the master does not inspect
Git directly. A quota-exhausted puppet can appear online and accept messages
while every model turn fails. A proxy outage goes to the operator, not a
restart. Never assume a silent node agent means a silent puppet.

One clone = one branch = one ticket. The puppet builds in the clone's default
build directory, not an improvised path or a cross-clone shared cache. Its
working branch is the ticket claim; create it before edits.

## Tracker and CI workflow

Only mop MCP tools access the tracker. Discover vocabulary with
`mcp__mop__dev_bug_labels`, search possible duplicates with
`mcp__mop__dev_bug_list`, inspect a ticket with `mcp__mop__dev_bug_show`,
and read epic relationships with `mcp__mop__dev_bug_epic`. A new unit of work
gets its own issue before implementation, via `mcp__mop__dev_bug_new`; edit
its evidence and scope with `mcp__mop__dev_bug_edit`, or leave a comment with
`mcp__mop__dev_bug_comment`. Use `mcp__mop__dev_bug_relabel` for permitted
labels, never to set/clear `status::wip` by hand: claim only after approval
with **`mcp__mop__dev_bug_start(no_branch=true)`**, so the master's shared
checkout is never switched; close with `mcp__mop__dev_bug_close` after
verified rollout. Keep exactly one `status::`, `sev::`, and `component::`
label. Titles, bodies and comments use the project's required language; in
mop they are Russian. A ticket body names reproduction, expected/actual,
evidence, confirmed cause when known and proposed work. One defect per
issue; check for duplicates first. Neither a dead node nor a full disk is a
tracker defect.

Before claiming an open ticket, rebuild the set from live
`mcp__mop__dev_bug_list` and `mcp__mop__dev_bug_show`, not a saved snapshot.
Where available, use `mcp__mop__dev_bug_evidence` to determine whether a
branch or implementing merge already exists. Its evidence is not proof of a
fix: read the ticket and the puppet's report, and distinguish instrumentation,
incomplete work and incidental issue mentions from a fix. If this tool is
missing and you cannot establish the status through supported MCP tools,
stop and ask the operator, not local Git or a tracker HTTP call. An epic
itself is not dispatchable work: its component tickets and trigger must be
explicit, closed and satisfied before it can land.

The integration pipeline belongs to the master. Use `mcp__mop__dev_ci_list`
and `mcp__mop__dev_ci_show` to track it, `mcp__mop__dev_ci_why` and
`mcp__mop__dev_ci_log` to explain failure, and `mcp__mop__dev_ci_retry` for
a cancelled/failed run when project rules warrant it. Consecutive pushes
may cancel prior runs; ensure the integration head has a pipeline that
actually ran, not merely a last cancelled one. Do not stretch landings
apart to save cancelled runs. Report CI failures and unverified rollouts
honestly; do not claim green from a puppet's source reading or an old run.

## Triage

Address the operator, not the compiler, in two passes. First, the essence
in three or four plain-language sentences **without code**: what the user
did, expected, got, and how often/whom it hurts, with a denominator when
available. Say what is actually happening in one phrase without function
names. Cover the rest of the triage with your hand: if the user's loss is
not clear from what remains, rewrite it.

Second, the implementation: file/line, one-layer fix shape, failing test's
contract, rejected alternatives and remainder. Technical detail moves into
the dispatch; it does not replace the operator's decision. The operator
approves the essence, not a diff. If refining a proposal grows it into a
multi-layer fix, split it into deliberate tickets instead of silently
expanding this one.

## Dispatch

Send full context; the puppet cannot read your triage and may wake blank.
Ask the puppet to report rather than ask its terminal's nonexistent human.
The following is **quoted instructions for the puppet**, not master shell
instructions. Adapt the gate and token clauses to the project's rules:

> Ticket #<id> — <title>. Directory: <your clone>. Branch:
> <ticket branch supplied by tracker claim>. Integration target:
> <branch from the master's profile, or the epic branch named by project rules>.
> I own tracker and CI; do not call tracker or CI commands yourself.
> Essence: <symptom and measured evidence>. Root cause (if confirmed):
> <file:line>. Approved fix: <one layer, exact scope>. Test first:
> <failing assertion>. Not doing: <rejected alternatives and why>.
> Protocol: failing test → minimal fix → narrow test → any project checks.
> Report fix branch and FULL fix sha, test name, unaddressed remainder, and
> a tracker comment in <project language>. If the fix does not close the
> ticket, say so. Ask me if a decision changes scope.
> Push the issue branch when ready, report it, and wait for my acceptance
> before landing. If the project keeps a token, wait for my grant before
> the merge/push; if it does not, wait for my explicit “land” message.
> Once authorized, fetch a fresh <integration target>; merge --no-ff,
> run <the project's required merged-tree gate, including mop dev test
> when its rules require the full suite>, then push once. A rejected push:
> fetch, merge again, rerun the merged-tree gate, push; never force push.
> Switch the clone back to its default branch when clean and report FULL
> merge sha, test verdict, remainder and rollout evidence. Do not wait for
> the integration pipeline — I monitor it. Return the token on your report.
> Reply via mcp__mop__send to the master's bus inbox from this message's
> `from-name` envelope; if lost, use the masters table in mcp__mop__agents.
> Never invent an address or ask your terminal's user; dismiss/avoid
> blocking dialogs and send me the question or guarded command instead.

Do not spell an old inbox out in the dispatch. If the puppet says its reply
was not delivered, a restarted master may have a new address: send the puppet
a new message and its envelope supplies the fresh address. A message from a
puppet is not acceptance; check its evidence and the approved scope first.

After dispatch, `mcp__mop__send(notify_when_idle=true)` may subscribe once
at the handoff; it reports the end of a *turn*, not completion. Do not poll
in a loop, resubscribe on every notice, or mistake idle for done. If a puppet
misses a deadline, inspect `mcp__mop__tail` before concluding anything.
A dialog blocks queued messages: dismiss via `mcp__mop__slash` with Escape,
never choose an answer from an unseen multiple-choice list. Repeated
blocking dialogs are a pool tooling defect; report them.

## Landing and rollout

Only the puppet who wrote the fix merges/checks/pushes. The master reviews
its report first and names the order when one issue depends on another:
“land only once #N is in the integration branch”; the puppet verifies that
ancestry before landing. An epic's tasks land into the epic branch named by
project rules, not straight into the default branch; its integrated epic
merge happens only at the operator's direction.

Where the project keeps a token, use `mcp__mop__landing(action="take",
puppet="<name>")` **before** authorizing a landing. Exactly one puppet
between merge and push; after the puppet's report use
`mcp__mop__landing(action="give")`. It is one shared token across masters;
a refusal naming another holder means wait or coordinate, not take it by
force. Only a permanently gone holder warrants forced recovery, disclosed
to other masters. Do not hold it through CI. Where project rules drop the
token, do not take one: Git rejects the losing push and the puppet redoes
the merge and gate. Landing permission is still explicit.

On the merged tree the puppet runs **all checks the project rules demand**.
For mop, that means `mop dev test` on the merged tree before its single
integration push, even if the suite is slower than lint; CI on the push is
additional, not a replacement. Other projects may permit a narrower gate;
read their rules before specifying it. Confirm that no competing run uses
the clone's build directory, run phases sequentially, and treat missing or
ambiguous completion markers as unverified, not green. A compile-capable
lint step need not be duplicated by a separate compile. Keep a warm landing
clone where practical, but never weaken required tests for speed. The
puppet reports the full fix and merge shas, checks actually run, and the
remaining work; the master does not infer completion from a process exit.

Close or transfer via `mcp__mop__dev_bug_close` or the appropriate tracker
MCP tool **after rollout is verified**, citing the fix/merge commits, the
guarding test, what remains, and actual verification. If rollout or CI is
still pending, leave the issue open and say so. A merged or pushed fix is not
by itself a deployment. Keep source changes and deployment ownership with
the puppet and operator according to the project's rules; server deployment
or repair is not an action for the master. Then return to the live roster
and live tracker queue for the next approved ticket.
