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
[BOOTSTRAP](docs/BOOTSTRAP.md), [CLUSTER](docs/CLUSTER.md), [LEASE](docs/LEASE.md), [AUTH](docs/AUTH.md), [PROXY](docs/PROXY.md),
[CLAUDECODE](docs/CLAUDECODE.md).

## New controller release (epic #623)

The Django release is a fresh-install replacement, not an in-place migration.
[API](docs/API.md) is its normative architecture/wire/authority contract;
[STORAGE](docs/STORAGE.md) maps state owners and removal;
[CONTROL](docs/CONTROL.md) maps preserved functions and permissions.
These documents specify the target, not functionality already implemented.

 - **MUST** Integrate this epic's tasks only into `epic/623-unified-pool-api`; do not merge to `master` or deploy on the old pool without a separate operator order. Before final integration, the operator must explicitly settle the old installation's automatic CI deploy.
 - **MUST** For new-release code, Django ORM owns durable mop state, Django Auth owns users/sessions/permissions, DRF owns commands/queries, and NATS/WS carry realtime only. CONFIG/STORAGE replace the old-runtime rules below that prescribe .env/inventory authority, private credential stores, connection.json login storage and file/NATS/HTTP state. Only their narrow protected DB/root/client bootstrap and generated external artifacts remain outside ORM; do not recreate the old stores in Django. The old-runtime rules still apply when maintaining the deployed version.
 - **MUST** New models and application transactions live in the server layer; common/client/node and stdlib-only session.py do not depend on Django. Follow the subject-module dependencies and invariant-owner matrix in API, with one mutation path shared by DRF/Admin/worker.
 - **MUST** Use portable ORM models/migrations/constraints/queries, PostgreSQL in production and its real concurrency gate; no vendor SQL or backend-specific business rules. Old script-suite green is not Django acceptance; #632 adds the runner, test database and clean-install gate.
 - **MUST** Keep admission/claim/result transactions short and synchronous, effects outside HTTP/SQL transactions in bounded workers, and async I/O outside blocking ORM code. Pass whole transactional functions through the safe sync/async boundary; never pass lazy QuerySets/connections across it.
 - **MUST NOT** Do not add old-data import, dual-write, file fallback, compatibility APIs, mixed-agent rollout or business effects in model.save/signals. Remove each old owner when its replacement is connected in the epic; preserve safety policies, not obsolete wire formats.

## Overall structure

 - `/mop` — the library: returns data, prints nothing; laid out by layers (#260): `mop/common` (everyone), `mop/client` (operator's machine and master shell), `mop/server` (controller), `mop/node` (agent); `mop/session.py` and `mop/usage.py` stay in the root (they travel to bodies by path), `mop/driver` is common with the node drivers inside, `mop/cli` holds the commands
 - `/bin` — one file, the `mop` launcher; the commandlets are modules of `/mop/cli`, one subcommand per module, in sections (`core`, `pool`, `service`) and groups (`cred`, `driver`, `node`, `project`, `secret`, `dev`, `server`); they are the only thing that prints
 - `/deploy` — the product's installation: Nomad, the bus, the agent, the disk watchdog
 - `/sandbox.yaml.example` — the puppet environment example: an installation copies it to `sandbox.yaml`, which git ignores like `.env` and `inventory.yaml`
 - `/skills/master` — the master session's skill, symlinked from outside
 - `/web` — the dashboard, a React app (Vite + TypeScript + Mantine) in `web/src`, built into the committed `web/dist` by `mop dev web build`; served by `mop server web`, node never reaches the servers
 - `/docs` — one file per subsystem
 - `/tests` — checks of pure functions, not a framework
 - Imports go downward only, common ← client / node / server; `tests/layers.py` derives the layer from the directory and refuses a module outside one, so a new module is placed in a layer deliberately

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
 - **MUST** Three sources, one question each: `.env` answers for the installation, the inventory for the machine, `config.SETTINGS` holds the defaults. `.env` never reaches a node — what the node must know is listed in `config.NODE_SCOPED` and rendered by `mop server deploy` into `~/.config/mop/node.env`. A node-side setting written anywhere else silently does not arrive
 - **MUST** A value specific to this machine is a setting whose default equals today's value, never a literal in the code
 - **MUST** A required setting with no sensible default goes in `config.REQUIRED`: silently walking into someone else's LAN is worse than a loud refusal
 - **MUST NOT** Nothing a specific project needs goes into `deploy/` (toolchain, env files, other people's MCP servers) — that is the installation's own `sandbox.yaml` and the projects' `.mop/sandbox.yaml` (baked) and `.mop/bootstrap.yaml` (played at every start)
 - **MUST** A project has one definition: `driver.project_of`, the origin's basename without `.git`; puppet names are built from it too. The word «shard» is gone (#85)
 - **MUST** Two layers: Nomad decides where a puppet stands, the bus decides how to talk to it. The Nomad token lives only on the server; neither `mop login` nor a node receives it
 - **MUST** The server is the ansible controller and the operator's machine is not in the inventory: it gets the server's credentials with `mop login` into `~/.config/mop/servers/[address]/`, and `MOP_SERVER_LAN` in the environment retargets a master at another server
 - **MUST** Symlinks pointing in from outside are interfaces: `~/bin/mop`, `~/.claude/skills/master`; deploy plays `deploy/site.yml` from the repository and writes nothing under `~/etc` (#311)
 - Terminology: **master** is the controlling side, **puppet** the working one; `free (master)` in a state string is a git branch, not a role

## Secrets

 - **MUST** Installation inputs live in the server's `.env`; generated credentials (NATS passwords, Nomad token, LLM proxy keys) stay in private server files. Client `mop login` saves bus and proxy credentials per server in `connection.json` (the legacy `operator.json` + `client.json` pair is still read for compatibility), never in installation `.env`
 - **MUST** `.env` never travels to the nodes — rsync excludes it explicitly
 - **MUST** No secrets in a Nomad job spec: it is visible in the UI and stays in the cluster's state; keys go to the nodes as a file, the spec carries only variable names
 - **MUST** A puppet reaches the bus under its project's credentials, not the node's: the agent sees who is being asked about, never who is asking
 - **MUST** Nodes are given nothing beyond their subjects: a new need is an agent verb, not a privilege
 - **MUST** The node-level verb `disk` lives in the `admin` pseudo-project only. `write` can deliver only paths from the node's own `WRITABLE` list; the cluster service uses it to distribute `secrets.env`, never a path supplied by a master (#284, #308, #393)

## Traps that cost debugging

Every one of them fails silently — hence a list, not "read the code".

 - **MUST** The wrapper lives in the job spec: editing `mop/server/spec.py` does not reach a running puppet through an allocation restart, it needs a re-registration
 - **MUST** Escape curly substitutions with a double dollar — Nomad runs the spec through hcl2 and parses the whole line, **comments included**
 - **MUST** The node agent lives outside the job spec, under systemd, one per node: otherwise every edit to it would re-register every job, and it must answer precisely while a puppet is restarting
 - **MUST** The agent's unit needs `XDG_RUNTIME_DIR=/run/user/1000`: without it `session.py` misses the socket directory and live puppets read as dead
 - **MUST** `AGENT SILENT` has no cure: the puppet may be working perfectly, and a restart would kill the work in its clone
 - **MUST** Address a puppet's session only through the freshest live one: files of dead sessions pile up, and the freshest may well be a corpse
 - **MUST** "free" means the clone holds no unsaved work, not that the session is silent: the dispatch decision rests on it
 - **MUST** Wrap MCP tool bodies in `loud`: an exception reaches the model as "Error executing tool [name]" — with no reason
 - **MUST** Never swallow an error on its way to the surface: a refusal or failure carries its reason to whoever reads it (CLI output, dashboard verdict, audit record). A sanitized allowlist is for untrusted raw exceptions, not a second pass over library phrases that are already safe — двойная санитизация и вырезанное поле error превращают «отказано» в глухое слово, и диагностика умирает на полпути
 - **MUST** The master's address is its bus inbox (`[login].[host]-[pid]`, the login as its subject token, #213), not the claude session name: that name is derived from a directory, is not unique and is unknown to the bus

## Strict TDD Protocol

**Mandatory for everything checkable without the pool** — pure functions
(`state.verdict`, `session.envelope`, `render.table`,
`gitlab.with_status`). The exceptions: what lives only on the live pool (the
wrapper, the playbooks, the agent's verbs), thin commandlet glue, mechanical
edits, documentation (prose needs no artificial failing check).

1. **Write a failing check first** in `tests/[module].py`, named for the defect or the property.
2. **Verify it fails** for the right reason (`python3 tests/[module].py`). If it passes, the check is wrong.
3. **Implement the minimal fix.** Track reasoning in test-file comments (`HYPOTHESIS:`, `SOLUTION:`, `RESULT:`) and undo every wrong-hypothesis change.
4. **Verify green** — the narrow check locally (`python3 tests/[module].py`), the full strict suite in CI on the pushed branch (#544), and leave `STATUS: FIXED — see #123` in the check.

 - **MUST** Stop and ask if you truly cannot write the check
 - **MUST NOT** Never implement a fix before the failing check
 - **MUST NOT** Never weaken a check to make it pass — assert correct behavior, never degraded-as-correct
 - **MUST** A missing local dependency is an environment defect, not a hand install and never a weakened check: a code-level declaration defect gets its own ticket; a dependency already declared but stale in an install is an operator blocker — it does not block the full CI gate. If a narrow check cannot run locally for an environment reason, push a test-only commit and verify its relevant failure in CI before implementing the fix; then verify green in CI. An import or setup failure is not evidence of the defect
 - **MUST** Verify on the live pool what cannot be verified otherwise (message delivery, puppet state, a rollout), and say in the report what you actually ran and what stayed unverified
 - **MUST NOT** Never pass reading the source off as an experiment: "the code says so" and "checked on a puppet" are different claims
 - **MUST NOT** Never fake a green run: there are no tests for the pool, and that is said plainly
 - **MUST** Breaking infrastructure changes carry a transition: the watchdog must keep seeing the old directories while someone's work is still in them

## Bug tracking

Work lives in GitLab issues. The coordinates come from the working copy's git origin, the credentials from `.env`.

 - **MUST** Use `mop dev bug` for every interaction with the tracker, never the API directly
 - **MUST** If a capability is missing, add it to `mop/cli/dev/bug` plus a check of its pure logic in `tests/gitlab.py`
 - **MUST** Extend the commandlet the moment it falls short, before going on: a comment instead of an edit, a curl instead of a verb, is the signal that a commandlet is missing, not a way around it (#288)
 - **MUST** Issue titles, bodies and comments are in Russian; code, branch names and commits stay English
 - **MUST** Every unit of work is an issue before the fix, in the fixed report shape: steps to reproduce, expected result, actual result, evidence, the root cause (only when confirmed) and what to do
 - **MUST** Exactly one label from each group `status::`, `sev::`, `component::` — a second of the same group silently replaces the first; `mop dev bug labels` prints the vocabulary
 - **MUST** One issue = one defect: split a compound one and cross-link the parts
 - **MUST** Search for a duplicate before opening (`mop dev bug list --all -t ...`): the test is the fix, not the wording — if one change closes both, it is one issue
 - **MUST** The master runs whatever commands it needs — the tracker and CI (`mop dev ...`), the `mop` CLI, Git, checks of a live installation (#450, #559). mop MCP tools stay the preferred path for pool operations: they carry ownership and the master's inbox address; a missing or failing tool is replaced by the same `mop` command in the shell, not by stopping. Operator-scale actions — a server's `.env`, a manual deploy where CI deploys, credentials, deleting puppets or data — run only on the operator's explicit direction, and secrets are never printed
 - **MUST NOT** The pool and its nodes are not tracker subjects: a dead node, a stale login, a full disk are an action on a machine, not a diff
 - **MUST NOT** Never set or clear `status::wip` by hand: `mop dev bug start` stamps it, `close` strips it; a stale wip is the only signal that work died

## Delivery

CI (`.gitlab-ci.yml`) runs `mop dev test` on every push, in Docker, on Python 3.12 (`test:py3.12`, with `MOP_TESTS_STRICT=1`, #228 — since #525 one test job: the server and nodes run the same `tests/` on their own 3.13, a job there caught nothing of its own), and `web:dist` rebuilds the dashboard with `mop dev web build --check` against the committed `web/dist` (#297): an edit to `web/src` ships with its rebuilt `web/dist` in the same commit, or the pipeline is red. The same holds for the CLI reference: `tests/cli.py` regenerates `docs/CLI.md` from the commandlets' docstrings (`mop dev docs`, #350) and compares it with the committed file, so an edit to a commandlet's docstring ships with the regenerated `docs/CLI.md` in the same commit, or the pipeline is red. **CI is the gate** (#544): the full strict suite and `web:dist` run in CI on the issue branch SHA and on the resulting integration head, not on the executor's machine — a local full suite is never a landing requirement, and a green pipeline, not a local run, is what a landing rests on. Green means the pipeline that ran on that exact SHA: stale or cancelled runs are not success. A red integration pipeline stops new landings until it is fixed or reverted by agreement, but never blocks independent work in issue branches. Code reaches the pool through `mop server deploy`, and `master` is the branch it is rolled out from: on an installation whose CI deploy variables are set, a green `master` pipeline runs it by itself (`deploy:mop`, a forced-command `mop deploy --from-ci` on the server, #239/#240; it needs `test:py3.12` and `web:dist`); elsewhere it is run by hand. `mop dev ci` reads pipelines, job logs and runners, and `mop dev ci lint` checks `.gitlab-ci.yml` before a push.

 - **MUST** Every task is a ticket first, and its branch carries the ticket number and a description: `[type]/[iid]-[slug]` (`mop dev bug start`), off a fresh integration branch
 - **MUST** A task that belongs to an epic integrates into the epic's branch `epic/[iid]-[slug]`, never straight into `master`: the executor lands there (the master names it as the landing target; `git config mop.branch` / `MOP_BRANCH` makes `mop dev bug start` base on it), CI runs `tests/` on every push, and the epic is tested in its branch until every task is done. Then the epic branch lands into the integration branch by one `merge --no-ff`, and only that merge is rolled out
 - **MUST** One fix = one layer: touching the wrapper, a playbook and the library at once is an unrevertable, unmeasurable change
 - **MUST** A commit explains why: the diff already shows what changed, and the incident behind the fix is worth more than a list of files
 - **MUST** Reference the issue in the commit subject as a bare `#74`, never a closing keyword
 - **MUST** Push the issue branch as soon as it is ready: work that lives only in a clone dies with the clone
 - **MUST** Land by local integration, one push: `git merge --no-ff` into a fresh `origin/master` (or your own branch when `git config mop.branch` / `MOP_BRANCH` names one, #249), the narrow checks available on the merged tree, then a single `git push`; one merge commit per issue keeps `git revert -m 1` as the rollback. The full strict suite and `web:dist` gate the landing in CI on the pushed merge SHA (#544) — a local full suite is not required and does not replace that pipeline
 - **MUST** Landing takes no token here (#225): git itself refuses the losing push of a race. A rejected push means fetch, merge again, rerun the narrow checks, push — never a force push. `mop landing` is for projects whose gate runs for tens of minutes
 - **MUST** If you touched what travels to the nodes (`mop/`, `deploy/`), make sure it is rolled out and check `mop list`: the nodes hold a COPY of the package, and an unshipped edit silently never arrives. Where CI deploys, watch the `master` pipeline's `deploy:mop` job in `mop dev ci` instead of running `mop server deploy` by hand — a manual deploy races the CI one; elsewhere run `mop server deploy` yourself
 - **MUST** Close the issue right after the rollout: `mop dev bug close [iid] --comment "…"` naming the commit and what it was verified with. A task of an accepted epic is ready for the epic's final acceptance once it is accepted, integrated into the epic branch and its integration CI is green — even while its issue stays open awaiting rollout; "close every issue before the epic merge" is not a cycle to enter when closing before rollout is forbidden. The epic branch lands into the integration branch and rolls out only at the operator's explicit direction, and the final master CI gate is what clears the rollout; live checks are not CI
 - **MUST** In pool mode the tracker belongs to the MASTER: the executor runs no `mop dev bug` at all and sends the comment text instead, which the master pastes
 - **MUST** In pool mode the master claims a ticket with `mop dev bug start [iid] [slug] --no-branch`: the executor creates the branch in its own clone, and the master's shared working copy is never switched. Before claiming, `mop dev bug evidence [iid]` shows an existing branch or integration merge — evidence to read, not proof of a fix (#560)
 - **MUST** In pool mode the executor lands on the branch the MASTER named and never picks the target itself
 - **MUST** In pool mode the executor lands only after the master accepted its report; an order between issues is the master's, named in the dispatch ("land only once #222 is in `origin/master`", checked with `git merge-base --is-ancestor`)

## Documentation

 - **MUST** A new subsystem is a `docs/NAME.md` file, one UPPERCASE word
 - **SHOULD** When the user types a bare UPPERCASE word (`BUS`, `DRIVER`), read `docs/[WORD].md`
