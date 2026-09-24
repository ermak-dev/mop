# Code Guidelines

mop — a pool of claude puppets on top of Nomad, a message bus on top of NATS.
The master hands out work to puppets — claude sessions on the pool's nodes.
**MUST** rules have already been paid for in debugging; **SHOULD** rules are
strongly recommended.

This file holds only what the repository cannot tell you by itself: structure
comes from `ls`, commands from `mop` with no arguments, the reason behind a
particular decision from the comment next to the code, subsystems from `docs/`:
[BUS](docs/BUS.md), [CHANNEL](docs/CHANNEL.md), [MCP](docs/MCP.md),
[GC](docs/GC.md), [DRIVER](docs/DRIVER.md), [WEB](docs/WEB.md),
[BOOTSTRAP](docs/BOOTSTRAP.md), [CLUSTER](docs/CLUSTER.md), [LEASE](docs/LEASE.md).

## Overall structure

 - `/mop` — the library: returns data, prints nothing
 - `/bin` — one file, the `mop` launcher; the commandlets are modules of `/mop/cli`, one subcommand per module, in sections (`core`, `pool`, `service`) and groups (`driver`, `bug`, `node`, `bootstrap`); they are the only thing that prints
 - `/deploy` — the product's installation: Nomad, the bus, the agent, the disk watchdog
 - `/sandbox.yaml.example` — the puppet environment example: an installation copies it to `sandbox.yaml`, which git ignores like `.env` and `inventory.yaml`
 - `/skills/master` — the master session's skill, symlinked from outside
 - `/web` — the dashboard page, served by `mop web`; no build step, no dependencies
 - `/docs` — one file per subsystem
 - `/tests` — checks of pure functions, not a framework

## Project environment

 - **MUST** Run everything through `mop [subcommand]` — the dispatcher sets PYTHONPATH, and without it a commandlet does not find the package
 - **MUST** A missing capability is a new commandlet in `mop/cli/`, never a workaround from outside
 - **MUST** The library returns data and stays silent; the frontends in `mop/cli/` print — otherwise a second frontend starts parsing text laid out for a terminal
 - **MUST** Output is English: it is read by the model through MCP, not only by a human. Comments and docstrings are Russian
 - **MUST** Commandlets never call each other as a subprocess: each parses its own arguments and prints for itself. The one caller of commandlets is the MCP adapter (#160): it runs a commandlet that declares `MCP = {...}` and hands its output to the model unparsed
 - **MUST** MCP never reimplements a pool operation: a management tool is a commandlet's `MCP` declaration, or the two copies drift apart
 - **MUST** `mop/session.py` is stdlib only, with no import from the package: it travels as source to wherever the session lives
 - **MUST NOT** Do not add emoji unless asked to
 - **MUST NOT** Do not write in caps: emphasis is the wording's job, not the shift key's. Caps are reserved for what is literally uppercase — identifiers, acronyms, status tokens (`FAILED`, `AGENT SILENT`) and the MUST/SHOULD keywords of this file

## Boundaries

 - **MUST** `mop/`, `docs/`, `skills/`, `deploy/` know no concrete host: anything installation-specific is a setting in `config.SETTINGS` or a line in `.env`
 - **MUST** Three sources, one question each: `.env` answers for the installation, the inventory for the machine, `config.SETTINGS` holds the defaults. `.env` never reaches a node — what the node must know is listed in `config.NODE_SCOPED` and rendered by `mop deploy` into `~/.config/mop/node.env`. A node-side setting written anywhere else silently does not arrive
 - **MUST** A value specific to this machine is a setting whose default equals today's value, never a literal in the code
 - **MUST** A required setting with no sensible default goes in `config.REQUIRED`: silently walking into someone else's LAN is worse than a loud refusal
 - **MUST NOT** Nothing a specific project needs goes into `deploy/` (toolchain, env files, other people's MCP servers) — that is the installation's own `sandbox.yaml` and the projects' `.mop/sandbox.yaml` (baked) and `.mop/bootstrap.yaml` (played at every start)
 - **MUST** A project has one definition: `driver.project_of`, the origin's basename without `.git`; puppet names are built from it too. The word «shard» is gone (#85)
 - **MUST** Two layers: Nomad decides where a puppet stands, the bus decides how to talk to it. The Nomad token lives on the server and, through `mop join`, on operators' machines; it never reaches a node
 - **MUST** The server is the ansible controller and the operator's machine is not in the inventory: it gets the server's credentials with `mop join` into `~/.config/mop/servers/[address]/`, and `MOP_SERVER_LAN` in the environment retargets a master at another server
 - **MUST** Symlinks pointing in from outside are interfaces: `~/bin/mop`, `~/etc/nomad`, `~/etc/nats`, `~/.claude/skills/master`; a playbook is found by the path `~/etc/[name]/setup.yml`, and there is no name table anywhere
 - Terminology: **master** is the controlling side, **puppet** the working one; `free (master)` in a state string is a git branch, not a role

## Secrets

 - **MUST** The project's secrets live in `.env` and nowhere else; what generates itself (NATS passwords, the Nomad token, the claude.ai login) never lands there
 - **MUST** `.env` never travels to the nodes — rsync excludes it explicitly
 - **MUST** No secrets in a Nomad job spec: it is visible in the UI and stays in the cluster's state; keys go to the nodes as a file, the spec carries only variable names
 - **MUST** A puppet reaches the bus under its project's credentials, not the node's: the agent sees who is being asked about, never who is asking
 - **MUST** Nodes are given nothing beyond their subjects: a new need is an agent verb, not a privilege
 - **MUST** The node-level verb `disk` lives in the `admin` pseudo-project only. `write` was taken out of that list deliberately — otherwise a project's master cannot `mop login` its own puppets; that is safe exactly while both `WRITABLE` files are assembled from the machine, not from the master's project

## Traps that cost debugging

Every one of them fails silently — hence a list, not "read the code".

 - **MUST** The wrapper lives in the job spec: editing `mop/spec.py` does not reach a running puppet through an allocation restart, it needs a re-registration
 - **MUST** Escape curly substitutions with a double dollar — Nomad runs the spec through hcl2 and parses the whole line, **comments included**
 - **MUST** The node agent lives outside the job spec, under systemd, one per node: otherwise every edit to it would re-register every job, and it must answer precisely while a puppet is restarting
 - **MUST** The agent's unit needs `XDG_RUNTIME_DIR=/run/user/1000`: without it `session.py` misses the socket directory and live puppets read as dead
 - **MUST** `AGENT SILENT` has no cure: the puppet may be working perfectly, and a restart would kill the work in its clone
 - **MUST** Address a puppet's session only through the freshest live one: files of dead sessions pile up, and the freshest may well be a corpse
 - **MUST** "free" means the clone holds no unsaved work, not that the session is silent: the dispatch decision rests on it
 - **MUST** Wrap MCP tool bodies in `loud`: an exception reaches the model as "Error executing tool [name]" — with no reason
 - **MUST** The master's address is its bus inbox (`[login].[host]-[pid]`, the login as its subject token, #213), not the claude session name: that name is derived from a directory, is not unique and is unknown to the bus

## Strict TDD Protocol

**Mandatory for everything checkable without the pool** — pure functions
(`state.verdict`, `session.envelope`, `render.table`,
`gitlab.with_status`). The exceptions: what lives only on the live pool (the
wrapper, the playbooks, the agent's verbs), thin commandlet glue, mechanical
edits.

1. **Write a failing check first** in `tests/[module].py`, named for the defect or the property.
2. **Verify it fails** for the right reason (`python3 tests/[module].py`). If it passes, the check is wrong.
3. **Implement the minimal fix.** Track reasoning in test-file comments (`HYPOTHESIS:`, `SOLUTION:`, `RESULT:`) and undo every wrong-hypothesis change.
4. **Verify green** — run every file in `tests/` and leave `STATUS: FIXED — see #123` in the check.

 - **MUST** Stop and ask if you truly cannot write the check
 - **MUST NOT** Never implement a fix before the failing check
 - **MUST NOT** Never weaken a check to make it pass — assert correct behavior, never degraded-as-correct
 - **MUST** Verify on the live pool what cannot be verified otherwise (message delivery, puppet state, a rollout), and say in the report what you actually ran and what stayed unverified
 - **MUST NOT** Never pass reading the source off as an experiment: "the code says so" and "checked on a puppet" are different claims
 - **MUST NOT** Never fake a green run: there are no tests for the pool, and that is said plainly
 - **MUST** Breaking infrastructure changes carry a transition: the watchdog must keep seeing the old directories while someone's work is still in them

## Bug tracking

Work lives in GitLab issues. The coordinates come from the working copy's git origin, the credentials from `.env`.

 - **MUST** Use `mop bug` for every interaction with the tracker, never the API directly
 - **MUST** If a capability is missing, add it to `mop/cli/bug` plus a check of its pure logic in `tests/gitlab.py`
 - **MUST** Issue titles, bodies and comments are in Russian; code, branch names and commits stay English
 - **MUST** Every unit of work is an issue before the fix, in the fixed report shape: steps to reproduce, expected result, actual result, evidence, the root cause (only when confirmed) and what to do
 - **MUST** Exactly one label from each group `status::`, `sev::`, `component::` — a second of the same group silently replaces the first; `mop bug labels` prints the vocabulary
 - **MUST** One issue = one defect: split a compound one and cross-link the parts
 - **MUST** Search for a duplicate before opening (`mop bug list --all -t ...`): the test is the fix, not the wording — if one change closes both, it is one issue
 - **MUST NOT** The pool and its nodes are not tracker subjects: a dead node, a stale login, a full disk are an action on a machine, not a diff
 - **MUST NOT** Never set or clear `status::wip` by hand: `mop bug start` stamps it, `close` strips it; a stale wip is the only signal that work died

## Delivery

There is no pipeline: code reaches the pool through a `mop deploy` run, and `master` is the branch it is rolled out from.

 - **MUST** Every issue lives on its own branch `[type]/[iid][-slug]` off a fresh `origin/master` (`mop bug start`)
 - **MUST** One fix = one layer: touching the wrapper, a playbook and the library at once is an unrevertable, unmeasurable change
 - **MUST** A commit explains why: the diff already shows what changed, and the incident behind the fix is worth more than a list of files
 - **MUST** Reference the issue in the commit subject as a bare `#74`, never a closing keyword
 - **MUST** Push the issue branch as soon as it is ready: work that lives only in a clone dies with the clone
 - **MUST** Land by local integration, one push: `git merge --no-ff` into a fresh `origin/master`, every file in `tests/` on the merged tree, then a single `git push`; one merge commit per issue keeps `git revert -m 1` as the rollback
 - **MUST** Landing takes no token here (#225): the whole check runs in about 20 seconds, and git itself refuses the losing push of a race. A rejected push means fetch, merge again, rerun `tests/`, push — never a force push. `mop landing` is for projects whose gate runs for tens of minutes
 - **MUST** If you touched what travels to the nodes (`mop/`, `deploy/`), roll it out with `mop deploy` and check `mop list`: the nodes hold a COPY of the package, and an unshipped edit silently never arrives
 - **MUST** Close the issue right after the rollout: `mop bug close [iid] --comment "…"` naming the commit and what it was verified with
 - **MUST** In pool mode the tracker belongs to the MASTER: the executor runs no `mop bug` at all and sends the comment text instead, which the master pastes
 - **MUST** In pool mode the executor lands on the branch the MASTER named and never picks the target itself
 - **MUST** In pool mode the executor lands only after the master accepted its report; an order between issues is the master's, named in the dispatch ("land only once #222 is in `origin/master`", checked with `git merge-base --is-ancestor`)

## Documentation

 - **MUST** A new subsystem is a `docs/NAME.md` file, one UPPERCASE word
 - **SHOULD** When the user types a bare UPPERCASE word (`BUS`, `DRIVER`), read `docs/[WORD].md`
