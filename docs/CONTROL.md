# Control plane

## Status and document ownership

This is the **future-release functional preservation contract** for #624 in
Django epic #623. It is not a description of a shipped Django service. The
source inventory below describes the pre-replacement implementation; [BUS](BUS.md)
and [WEB](WEB.md) still document that implementation, including historical
transitions. Their NATS RPC and stdlib HTTP instructions are not instructions
for the future release.

The replacement targets a fresh installation. It makes no compatibility promise
for old CLI syntax, NATS subjects, HTTP paths, credentials, on-disk formats or
mixed-version deployments. Preserving a function means preserving its outcome,
authority boundary, safety checks and observable failure, not retaining its old
transport. This does not authorize deleting an existing installation's work.

The parent epic's [API](API.md) is the canonical wire and authority contract;
[STORAGE](STORAGE.md) owns persistence, transactions and recovery. Endpoint labels
in the inventory tables are semantic DRF resource/action names. Their route
binding is specified below under API.md's `/api/v1/` and `/ws/v1/` namespaces;
serializers, envelopes, authentication, authority and outcomes follow API.md.
A mismatch is an integration gap to resolve, not permission to implement two
variants. These are specifications, not evidence that an API or schema exists.

## One authority, three interfaces

Django REST Framework (DRF) is the authenticated control boundary for the web,
CLI and MCP frontends. They call the same application operations; none shells
out to another frontend or recreates its authorization. Django's database owns
control-plane records according to STORAGE.md. The scheduler owns placement;
node agents report live facts and execute bounded effects. A running allocation
is not proof of a ready role session, and a failed observation is not proof that
an effect did not happen.

NATS remains an **internal realtime transport only**. It is not a public control
API, CRUD backend, task queue, durable operation store or source of permissions.
In particular, the old `cluster.rpc`, `server.rpc`, `build.rpc` and agent RPC
subjects are not an alternative route around DRF. Scheduling work and recovering
its outcome use the execution and persistence mechanism specified by API.md and
STORAGE.md, not an implicit NATS request queue. Realtime notifications can prompt
a refetch; a missed notification cannot erase a record or justify replaying an
effect. Node authentication and the authenticated internal execution path must
remain distinct from browser authentication.

Django ASGI with Channels terminates browser realtime connections. The browser
receives no NATS URL/password, service credential, scheduler token, role grant
credential or proxy management key. Channels groups are server-selected scoped
subscriptions, not client-selectable NATS subjects. A group name, target name,
request body `owner`, role name or `from` field cannot establish authority.

### Permission notation

The following labels describe required checks, not role names. The permission
vocabulary below supplies ordinary Django permission codenames for the owning
modules; API.md defines their authority and enforcement. No codename alone
bypasses membership, incarnation, ownership, grant or safety checks.

* **Read(project)**: authenticated identity with read access to the actual
  project. Installation-wide read requires explicit authority. Any retained
  anonymous overview is a separately defined public projection, not this scope.
* **Act(project)**: authenticated human authorized for that project, with fresh
  target, clone and ownership checks. Human admin is not a role puppet.
* **Grant(capability)**: an authenticated agent Actor acting under a live
  delegation, resolved against its current delegation, human owner, project,
  job, incarnation and pinned role digest. Only the allowed operation is
  delegated; authority fields supplied by the caller do not override the
  resolver, and the delegation narrows — never widens — the Actor's rights.
* **Admin**: currently authorized human installation administrator. A puppet
  called `master` or `builder` does not qualify. Node-wide facts and cross-project
  effects need this authority.
* **Private terminal**: explicit trusted human operator capability, initially
  admin-only, additionally bound to the authenticated client/session and live
  target. Target role capabilities do not grant a viewer access.
* **Local operator**: an explicit local installation/bootstrap or recovery action
  on the appropriate machine. OS/controller access is not a remote role grant.

All remote mutations enforce their permission again at the effect boundary,
fail closed when identity or facts cannot be established, and retain the safe
reason for refusal. Project ownership comes from authoritative origin/target
records, not a `pu-` name prefix. Destructive/privileged overrides are explicit,
separately authorized and audited; they are never a default GUI capability.

### Django action permissions

App labels follow the owning modules in API.md; the codenames below do not
require one Django app per row. Register permissions with migrations, assign them
through Django groups/users and check the same action in DRF, Admin, workers and
Channels. `is_staff` is not a physical-operation permission. A superuser still
passes identity/fencing/safety checks. Standard model view/add/change/delete
permissions govern configuration editing only; custom actions below govern
physical effects and private data. No public overview is enabled by default in
this contract; an anonymous projection requires an explicit API policy decision.

| Owner | Action codenames | Additional scope/gates |
|---|---|---|
| Catalog | `view_pool`, `view_project`, `configure_project`, `set_project_limit` | Read(project) for scoped views; Admin for project registration/removal/limits. Configuration permission cannot start a build. |
| Catalog / Operations | `view_node`, `configure_node`, `drain_node`, `enable_node`, `forget_node`, `build_project`, `recover_build` | Admin; build recovery is distinct from an ordinary build. |
| Sessions / Operations | `create_puppet`, `update_puppet`, `restart_puppet`, `recycle_puppet`, `delete_puppet`, `wipe_puppet`, `stop_allocation` | Act(project), or corresponding resolved Grant operation; clone/assignment/execution fences. |
| Operations | `stop_puppet_job`, `override_assignment`, `recover_operation`, `cancel_operation` | Admin for job stop/privileged recovery/override; cancellation needs current access to the operation and the stage rules in API.md. |
| Sessions/delivery | `send_message`, `dispatch_task`, `view_delivery`, `register_endpoint`, `deliver_report` | Human/grant semantics differ; exact endpoint owner and recipient; dispatch requires the declared dispatch capability and receiving target. |
| Sessions/delivery | `view_history`, `view_tail`, `read_terminal`, `control_terminal`, `send_session_command`, `change_session_model`, `view_usage` | Authorized target/project; terminal read/control initially human Admin only and session-bound. History/tail initially web Admin; any later non-admin access requires explicit permission and scope. |
| Sessions / Landing | `view_landing`, `acquire_landing`, `release_landing` | Current project-scoped read; mutations require the actual current human Actor, exact epoch/revision CAS and own-owner release. No delegation, force override or external Operation is bound (#638). |
| Access / Preparation | `view_role`, `approve_role`, `issue_grant`, `revoke_grant`, `approve_bootstrap` | Approved human authority; role grants never self-approve policy. Read catalogue/profile is scoped and credential-free. |
| Secrets | `view_secret_metadata`, `manage_project_secret`, `deliver_secret`, `rotate_proxy_key` | Human project secret management; internal addressed delivery only; Admin for installation proxy rotation. Metadata view cannot retrieve bytes. |
| Operations | `view_operation`, `view_audit`, `diagnose_pool`, `treat_pool`, `collect_pool_garbage`, `sweep_pool`, `view_node_disk` | Operation actor/project audience; private audit and installation maintenance initially Admin. Preview is a read with the same cross-tenant scope, not a bypass. |
| Composition / proxy gateway | `manage_proxy`, `view_models` | Current human Admin for management; authorized inference catalogue for models. Neither permission exposes upstream keys. |

These action permissions do not replace the agent definition's finite capability
allowlists. A grant request must pass both its delegated operation mapping and
current owner/project authority; it receives no Admin, secret-management or
private-terminal privilege merely because its human owner has one. Internal
bootstrap/node effects authenticate service identity and assigned target, not
an unrestricted human action token.

### Catalog service action mapping (#634)

`mop.server.catalog.permissions.ACTION_PERMISSIONS` is the catalog-owned,
immutable mapping from each emitted service action to the **tuple of all required
Django permissions**. An action label is not necessarily a single permission.
The #633 authorizer consumes this data and separately checks current identity,
server-resolved scope and the gates above; the mapping is not a scope evaluator
or an authorization backend. Unknown actions have no implicit model-permission
fallback. The catalog's unbound authorizer still denies every call.

The following table describes the catalog service contract. Every name in both
columns has the `catalog.` prefix; comma-separated requirements mean **all**, not
alternatives.

| Service action | Required Django permissions |
|---|---|
| `add_project` | `add_project`, `configure_project` |
| `change_project` (origin/name) | `change_project`, `configure_project` |
| `delete_project` | `delete_project`, `configure_project` |
| `set_project_limit` | `change_project`, `configure_project`, `set_project_limit` |
| `add_node` | `add_node`, `configure_node` |
| `change_node` (addresses, deployment membership, desired eligibility, project links) | `change_node`, `configure_node` |
| `delete_node` | `delete_node`, `configure_node` |
| `change_nodesetting` | `change_nodesetting`, `configure_node` |
| `observe_node`, `record_node_application`, `record_settings_application` | Each action's own permission only |
| `pin_project_policy` | `pin_project_policy` |
| `view_project`, `view_node`, `view_installationsetting`, `view_nodesetting`, `view_projectpolicysnapshot` | Each action's own standard model permission |
| `change_installationsetting`, `add_imagerevision`, `change_nodeimage` | Each action's own standard model permission |
| `add_clientregistration`, `change_workspacebinding`, `view_workspacebinding` | Each action's own standard model permission |

`configure_project` and `set_project_limit` belong to Project; `configure_node`,
`observe_node` and `record_node_application` belong to Node;
`record_settings_application` belongs to InstallationConfiguration (for both
installation and addressed-node settings receipts); `pin_project_policy` belongs
to ProjectPolicySnapshot. These seven custom permissions are declared in model
metadata and the unified fresh-install migration `catalog.0002_action_permissions`; Django's ordinary
`migrate`/`post_migrate` creates the assignable Permission rows. Standard
`view_project` and `view_node` remain autogenerated, not duplicate custom rows.

Observation/application permissions permit only their addressed facts/receipts,
not configuration edits, arbitrary targets or physical effects. Scope remains a
separate #633 decision. Pinning policy records exact input, not approval or a
build; confidential policy access also performs the service's explicit project
and policy view checks. Image intent/availability methods edit metadata only.
Changing desired node eligibility or deployment membership does not execute a
drain/restart/deploy or prove application. Project/node removal is metadata
retirement, not physical deletion: the catalogue retains target IDs and referenced
history, hides retired targets from active queries and assigns a new UUID when a
name/origin is registered again. Retirement defaults to refusal until the trusted
removal guard is bound. `delete_node` is not permission to execute the future
physical forget operation. No configuration permission or
change flag implies `build_project`, `drain_node`, `enable_node`, `forget_node` or
any puppet lifecycle permission. The unified auth migration also preserves the
additional CONTROL action permissions (`view_pool`, `build_project`, `recover_build`,
`drain_node`, `enable_node`, `forget_node`); declaring them does not implement their
physical-operation handlers. Those handlers remain with their implementation tasks.

The PostgreSQL catalog checks exercise default migration permission creation,
User/Group assignment and a test-only Django-permission authorizer. They do not
claim that #633's production scope/grant integration or remote API is complete.

### Endpoint bindings

All paths here are **future-release endpoints**, not existing routes. `{id}` and
nested IDs are opaque resource identities; authorized lookup and foreign-key
validation follow API.md. Configuration CRUD uses GET/POST/PATCH/DELETE with
standard finite schemas and appropriate configuration permissions. Physical
effects use POST command endpoints with API.md's request identity, deadline,
revision/incarnation and durable operation response; they are not generic RPC.
Only the listed actions are valid; `{action}` is route notation, not arbitrary
caller-selected Python, shell, bus subject or handler name.

| Semantic inventory labels | DRF / Channels binding |
|---|---|
| `projects.list/retrieve/create/destroy`, limits | `/api/v1/projects[/{id}]`; `GET/PATCH /api/v1/projects/{id}/limit` |
| `projects.builds.create/recover`, build result/progress | `POST /api/v1/projects/{id}/commands/build`; `POST /api/v1/builds/{id}/commands/recover`; `GET /api/v1/builds/{id}` and `/progress`; private invalidation through events |
| `nodes.*`, pool | `/api/v1/nodes[/{id}]`; `POST /api/v1/nodes/{id}/commands/drain`, `/commands/up`, `/commands/forget`; `GET /api/v1/nodes/{id}/disk`; `GET /api/v1/pool`; metadata via configuration PATCH |
| Project secrets | `GET/POST /api/v1/projects/{id}/secret-files` and `/secret-vars`; `DELETE` the addressed secret ID; GET returns metadata only, no value detail/download |
| `puppets.*` reads | `GET /api/v1/puppets[/{id}]`; bounded `/allocation`, `/state`, `/clone`, `/sizes`, `/tail`, `/bootstrap-result` detail queries |
| Puppet creation and lifecycle | `POST /api/v1/puppets`; `POST /api/v1/puppets/{id}/commands/{action}` for `update`, `restart`, `recycle`, `delete`, `wipe`, `stop-allocation`, `stop-job` only; explicit takeover parameters need `override_assignment` |
| Messages, dispatch, reports and receipts | `POST /api/v1/messages`, `/dispatches`, `/reports`; `GET /api/v1/deliveries/{id}`; operation lookup for admitted effects; private receipt/report invalidation via scoped events, content through authorized reads |
| Live addresses/endpoints | `GET /api/v1/addresses[/{id}]`; authenticated `POST /api/v1/endpoints`, addressed renew/delete actions; current endpoint credential/owner and live incarnation checked |
| Profile, roles, grants | `GET /api/v1/identity`; `GET /api/v1/roles[/{id}]`; explicit `POST /api/v1/roles/{id}/commands/approve`; `GET/POST /api/v1/grants`; `POST /api/v1/grants/{id}/commands/revoke`; credential cut is an explicit revocation option, never a reusable-name revoke |
| Bootstrap approval | `POST /api/v1/puppets/{id}/commands/approve-bootstrap`; internal assigned-node bootstrap/receipt actions are service-only, not anonymous public bootstrap |
| Landing | `GET /api/v1/projects/{id}/landing`; `POST` its `/commands/take` and `/commands/release` |
| Private history and terminal | `GET /api/v1/sessions/{id}/history`; `POST /api/v1/sessions/{id}/terminal-control` to acquire, addressed renew/release; `/ws/v1/terminal/{session-id}` for the private live stream, as specified in API.md |
| Restricted commands and model | `POST /api/v1/sessions/{id}/commands/slash` and `/commands/model`; `GET /api/v1/models` |
| Usage (#656 supersedes transcript attribution) | `GET /api/v1/usage` with UTC `[start,end)` window, bounded cursor pages and daily/source/key/model dimensions. Requires global `execution.view_usage`; shared-key user/project/puppet attribution is unavailable, not an inferred project filter. Nullable categories retain known/unknown event coverage. |
| Maintenance | `GET /api/v1/maintenance/diagnoses`, `/api/v1/maintenance/gc-preview`, `/api/v1/maintenance/sweep-preview`; `POST /api/v1/maintenance/commands/{action}` for `treat`, `gc-run`, `sweep-run` only; mutation results through operations, previews are bounded read-only queries |
| Proxy-key rotation | `POST /api/v1/proxy-rotations`; `GET /api/v1/proxy-rotations/{id}`; explicit `/commands/resume` |
| Operations, journal, snapshot and events | `GET /api/v1/operations[/{id}]`, request-id lookup per API.md, explicit cancel/reconcile commands; `GET /api/v1/journal`, `/api/v1/snapshot`; `/ws/v1/events` carries safe scoped invalidations only |
| Auth, readiness, proxy gateways | Django login/logout with CSRF and DRF token issuance/revocation under API.md; `GET /api/v1/health`; protected `/llm/management.html` and `/v8/management/*` gateway, separate inference `/v1/*` |

Browser terminal control is acquired through DRF, not via an event-group join.
Internal registration/bootstrap/telemetry endpoints require service authentication
and narrowly typed assigned-target schemas defined by their implementation task;
these must be enumerated before that task is accepted, never replaced by a
catch-all verb relay. Local setup, bootstrap, node driver, launch and diagnostic
commands below remain local responsibilities rather than fabricated REST routes.

## Functional preservation inventory

Each row names the old entrypoint, its replacement surface and its minimum
permission. Realtime is supplementary unless the row explicitly describes a
private stream. Absence of an old GUI button is not a reason to omit a CLI/MCP
function from the replacement.

### Projects, images, nodes and secrets

| Old entrypoint / source operation | Future DRF endpoint or local command responsibility | Permission and preserved behavior |
|---|---|---|
| `mop project list`; cluster `projects` | `projects.list/retrieve` | Read access to the authorized registry projection; do not expose other projects through counts, origins or errors. Admin gets installation inventory. |
| `mop project add [origin]`; `project_add` | `projects.create` followed by `projects.builds.create` | Admin; validate repository and manifest before reporting readiness. Registration and a usable image are separate results, not one misleading success. |
| `project add --update`, `--rebuild`; builder `build` | `projects.builds.create`, `builds.retrieve`, private build progress subscription | Admin; retain `missing` (only absent images), incremental `update`, and base-image `rebuild`. No container nodes means no image work, not an invented failure. |
| `project add --branch NAME --node NAME` | Same build endpoints with validated branch and node selection | Admin; resolve remote branch to a verified commit; explicit branch implies an update even in missing mode. One image per project/node is replaced, not a branch-named parallel image. Build branch does not change working or landing branch. Scope stop/recreate effects to the selected node; reject unknown/non-container nodes, never silently fall back to all nodes. |
| Controller `mop driver build [--fresh] [--force]`; image clear/recreate | `projects.builds.create` with approved source snapshot; explicit privileged `projects.builds.recover`, or narrowly audited local controller repair per API.md | Admin plus explicit recovery approval for force; ordinary builds refuse busy/unknown clones. Preserve controller-local working-tree manifest input versus origin/default-branch input, and explicit remote branch precedence, recording exact accepted bytes/provenance. Preserve the affected-body report, pinned role metadata, approved workspace and recreation results, including failure after removal. Recovery must name the work it can destroy; it must not be hidden behind retry or rebuild. |
| `mop project delete`; `project_delete` | `projects.destroy` | Admin; refuse while jobs still exist, including stopped jobs. Remove associated policy/secret references deliberately, with storage cleanup owned by STORAGE.md. Do not sever live actors' authority implicitly. |
| `mop project limit [name N\|none]` | `projects.limits.list/update` | Admin; nonnegative count, zero freezes creation, none removes limit. Count and reservation checks must serialize with creation/deletion. Lowering the limit does not kill existing bodies. |
| `mop node`; cluster `nodes`, `pool`, `meta` | `nodes.list/retrieve`, `pool.retrieve`, privileged `nodes.metadata.update` | Admin for node-wide drivers, projects, capacity and metadata mutation; authorized pool projection may expose less. Unknown drivers/agents remain explicit errors. |
| `mop node drain`, `up`, `forget` | `nodes.drain`, `nodes.up`, `nodes.destroy` | Admin; drain closes placement and relocates gracefully with clone/work retained; up reopens placement. Forget refuses live occupants and nodes still in installation inventory, so the next deployment cannot silently recreate them. |
| `mop secret file add/list/remove`; `secret_put/list/remove` | `projects.secret-files.create/list/destroy` | Human project secret-management authority; Admin only in an explicitly selected project, not a global secret namespace. Upload clone-relative regular files with canonical names, reject traversal/symlinks. List metadata (name/size), never download values through a list/read endpoint. |
| `mop secret var add/list/remove` | `projects.secret-vars.create/list/destroy` | Same project secret authority; validated variable names, write-only values, names-only listing. Never return secrets in operation receipts, audit, public events or logs. |
| Bootstrap role secret staging and node `write` for `secrets.env` | Internal authenticated bootstrap/delivery operations; DRF exposes delivery status, not arbitrary remote file writes | Assigned node/body identity and current pinned role allowlist; only declared files/vars reach that body. Keep cleanup of removed staged secrets. Server-controlled proxy-key delivery uses fixed node-owned writable paths, not a browser-supplied path. A metadata permission does not grant delivery or secret retrieval. |

### Puppets, delegation and local launch

| Old entrypoint / source operation | Future DRF endpoint or local command responsibility | Permission and preserved behavior |
|---|---|---|
| `mop list`; MCP `agents`; cluster `roster/alloc/spec`, agent `state/states/clone/sizes` | `puppets.list/retrieve`, `puppets.allocation`, `puppets.state`, `puppets.clone`, `puppets.sizes` | Read(project), or Grant for each permitted read. Include stopped, queued, starting, failed bootstrap and unknown observations. Sizes are demand-driven expensive facts. Preserve dirty/ahead/lease facts; silence is not idle/free. |
| `mop add --role ROLE --request-id ID` | `puppets.create`, `operations.retrieve` | Act(project), or Grant with create and the requested `spawn` role allowed. Every body has a role; pin bundle digest and incarnation, verify returned binding. One creation key reconciles an unknown response to the reserved job/grant; changed payload under that key refuses before effect. Never blindly create a second body. |
| `mop update [origin] [--fresh]`; GUI keep/replace/remove policy modes | `puppets.update` operation | Act(project) or permitted Grant; omitted fields retain origin/policy/branch as applicable; explicit fresh changes conversation. Human policy replacement records provenance and approval; a grant cannot upload policy and must retain the approved live snapshot. Origin changes cannot escape project scope. Re-registration differs from an allocation restart. |
| `mop restart`, `recycle`, `delete`, `wipe` | `puppets.restart/recycle/destroy/wipe` operations | Act(project) or exact Grant capability; fresh facts and lease gates before effects. Restart retains clone/branch; recycle destroys unsaved clone/target state; delete removes job/body; wipe requires the body stopped and resets its working copy. Refuse ambiguous liveness and foreign work. Privileged takeover is distinct and audited, not silently inherited by the web. |
| Cluster `stop` (allocation stop) | `puppets.stop-allocation` | Act(project) or exact Grant capability; ownership and effect fences. Stop the current allocation, not the job's desired lifecycle; scheduler restart/rescheduling remains possible. |
| `mop stop`; cluster `job_stop`, bootstrap `give_up` | `puppets.stop-job`; internal bootstrap failure handling | Admin for human job stop; internal service authority for bounded give-up. Preserve clone/body/workspace, stop initialization/session/restarts. This is not allocation stop, an interrupt keystroke or puppet deletion. Do not expose internal give-up as a project role privilege. |
| MCP `send`, `mop send`; agent `send` | `messages.create`, `deliveries.retrieve`, scoped receipt subscription | Human Act(project) can claim the lease under existing safety rules; Grant(send) sends a plain message without taking lease/git identity. Preserve priority, delivery refusal, message id, bounded wait and idle-notification options. Reports to a master use a discovered live address, not its display name. Local UDS delivery remains local, not a server endpoint accepting arbitrary socket paths. |
| MCP `dispatch`, agent dispatch transaction | `dispatches.create`, dispatch receipt | Grant(dispatch), same project and target `receives_dispatch`; explicit target or safe free/no-lease autopick. Atomically gate clone/lease, install the human owner's git identity and deliver. `spawn` does not define dispatch targets. A role's send must not become dispatch by changing a flag in the message body. |
| `mop landing show/take/give/recover-take/recover-give <project-id>` (#642) | `projects.landing.retrieve/take/release` | Explicit canonical project UUID; mutations and recovery require `--epoch` and `--expected-revision`, explicit recovery also the original actual human `--owner-id`. Canonical `execution.view_landing/acquire_landing/release_landing`; MCP conservatively requires all three and a human actor. No puppet-to-Actor guessing, delegation, force, local context or external Operation. Only a complete command response acknowledges SQL CAS. Lost-response recovery is GET-only by exact successor revision/owner and remains an observation (`command_outcome: unknown`, exit 3), never a historical receipt or automatic retake; a later revision for the same owner does not match (#638). |
| `mop profile`; role catalogue and grant resolution/revoke | `identity.retrieve`, `roles.list/retrieve`, `grants.retrieve/revoke` | Own authorized profile/catalogue; only approved human authority creates/revokes delegations. Do not reveal credentials. Revocation names the exact Actor/delegation/incarnation, never a reusable puppet name, and deletes neither the Actor nor its shared token; body may keep running but new effects fail closed. |
| Role bootstrap, `identity`, `resolve`, `bootstrap_result`, `startup` | Internal authenticated registration/bootstrap/identity operations; `puppets.bootstrap-result` for authorized observation | Assigned node and exact live binding; human owner supplies git identity, not the role request. Return pinned role/bundle identity and approved policy provenance. Grant child creation inherits the parent's approved snapshot and cannot escalate `spawn`, project, owner or secrets. |
| `mop <role> [origin]`, `mop master` | Local role launcher calling `puppets.create`, readiness and terminal attach | Authorized human launch; always create a new role body, wait for matching ready binding, then attach. Do not choose an existing puppet merely by role name. Failure after creation names the existing body for recovery instead of creating another. |
| `mop attach <name>` | Local terminal client over the canonical private attach interface (or explicitly approved SSH transport) | Explicit target access and trusted terminal authority; creates no body. Detach/disconnect does not stop the job. A legacy SSH helper is not a reason to expose node shell argv over DRF. |
| `mop code [options]` | Local non-pool launcher; selected server inference configuration | Inference credential scope only; pass local CLI options through. No project, pool grant, bus credentials or pool MCP tools. Local execution is not a controller job or remote shell endpoint. |

### Private session access and realtime conversation

| Old entrypoint / source operation | Future surface | Permission and preserved behavior |
|---|---|---|
| `mop tail`, MCP `tail`; `/api/puppet/<name>[/tail]` | `puppets.tail` / startup diagnostics | Authorized private project/session read, initially web Admin. Bounded snapshot, explicit truncation, initialization/stderr fallback; not a terminal emulator or success signal. |
| `mop session history`; `/api/puppet/<name>/history` | `sessions.history.list` | Explicit private history permission on the authorized live target. Bounded records, cursor, epoch, session id, reset, has-more and partial. No arbitrary transcript path; incomplete JSONL remains pending and a new conversation resets the cursor. |
| `mop session terminal open/read/input/interrupt/resize/close`; old HTTP `term/*` | DRF `sessions.terminal.open/close` control and private Channels terminal connection, as bound by API.md | Private terminal authority. One live foreground conversation/PTY identity, explicit reader/writer mode, one writer per target across all ASGI workers. Raw PTY bytes are private, not capture-pane text or an administrative operation queue. |
| MCP `slash`; agent `type`; `models`, model switch | `sessions.commands.create`, `models.list`, `sessions.model.update` | Act(project) or exact Grant capability. Preserve the allowlisted slash/Escape path and observed model-switch result. Do not turn a restricted command endpoint into arbitrary shell execution. Private terminal input is a separate trusted capability. |
| MCP `agents`, `who`, live master inbox and local session discovery | DRF `addresses.list/retrieve`, `reports.create`, `deliveries.retrieve`; scoped Channels presence/report/receipt stream; local-only session listing | Authorized project/principal audience. Distinguish stable job, incarnation, conversation, live delivery address and human owner. Local host sessions cannot be enumerated remotely without an explicit local bridge. |
| `notify_when_idle`, idle notice and master report | Scoped Channels receipt/report notification plus retrievable delivery/receipt record | Sender/recipient access checked by Django. Report delivery and idle receipt are distinct: receipt means the observed session went idle/free, not that a task was correct. Lost address, disconnected recipient, timeout, partial and unknown remain visible. Never invent a live address from a session name or return a stale route as deliverable. |
| Old `/events`, pool snapshot and journal | DRF pool/journal/operation reads; separately authorized Channels public-projection, project and private operation subscriptions | Public projection only if explicitly allowed by API.md; private records require audience/project permission. Reauthorize subscriptions and redact payloads at the server. Public journal never carries history, terminal ids/bytes, message text, bootstrap or secret values. |

#### ASGI terminal lifecycle

Channels is the browser-facing transport, not a second session implementation.
History and the terminal refer to the same existing foreground process and live
conversation. Do not start a second model process for chat or persist terminal
keyboard input as a queued operation. The UI has one active target connection;
switching target closes the previous attachment. Reader access cannot type or
resize, and writer reservation is enforced centrally, not by an in-process lock
in each web worker.

Bind an attachment to authenticated Django session/client identity, actor,
project, target, incarnation, live conversation and terminal epoch. A handle or
WebSocket URL alone is not a bearer capability. Resolve the node/body server-side;
clients cannot choose an agent, tmux target, file path or attach command. Cookie
HTTP mutations require Django CSRF validation and the configured same-origin
policy; WebSocket upgrades validate Origin and the session-bound authorization
mechanism specified by API.md. SameSite cookies alone do not replace CSRF or
WebSocket Origin checks. Non-browser clients use their own explicit auth scope,
not a forged browser cookie or CSRF bypass.

Recheck current permissions on connect and throughout long-lived use, including
before input/resize and before releasing a delayed read/frame. Logout, expiry,
permission/grant revocation, identity change and conversation replacement revoke
access and detach the attachment. Disconnect, target switch, abandoned handles,
server shutdown and node-side timeout all have bounded cleanup. Cleanup releases
the attachment and writer reservation, not the working session/job. A failed
cleanup is an observable failure, not an assertion that control was released.
No revoked connection may continue receiving buffered private output.

Preserve bounded output/backpressure, byte cursors, reset/closed/exit states,
validated dimensions, input size limits and consecutive input sequence fences.
Duplicate identical input may return its prior receipt; a reused sequence with
different bytes refuses. An unknown input receipt must not trigger replay with
a new sequence. Accepted bytes do not prove the CLI executed a command or stopped
a tool. `/clear` can change conversation identity without changing PID/socket;
old writer authority must not follow automatically. History and terminal epochs
are different namespaces. Terminal-control audit contains actor/target and
acquire/release outcome, never bytes or authentication handles. Raw terminal
access can execute code in the body and cannot promise secret redaction.

### Usage, maintenance, bootstrap and proxy

| Old entrypoint / source operation | Future DRF endpoint or local command responsibility | Permission and preserved behavior |
|---|---|---|
| `mop stat`; legacy `--puppets/--users` and agent token collector removed at connected cutover | `usage.list/aggregate` from CLIProxyAPI only (#656) | Global `execution.view_usage` and installation scope. UTC time/key/model, parent totals or normalized nonoverlapping components, nullable category coverage and explicit source gaps. Shared-key user/project/puppet attribution is unavailable. No history import, transcript fallback, current-owner guesses or monetary billing promise. #626/#642 bind the public DTO; #669 binds the sole-consumer credential port. |
| `mop diagnose`, `mop server diagnose` | Local client/controller diagnostics; `health.retrieve` for service readiness | Local operator; offline login/config provenance checks remain possible when the API is down, without printing secrets. Remote readiness states what was observed, not merely that the process exists. |
| `mop server doctor [group] [--fix --safe]` | `maintenance.diagnoses.list`, `maintenance.treatments.create` | Admin; diagnosis separate from treatment, selected groups retained, safe-only policy retained. Silent agent has no puppet restart cure. Unsafe restart/model/spec changes require deliberate authority and fresh safety checks; scheduler queue explanation is not a session failure. |
| `mop server gc [--dry]` | `maintenance.gc.preview/run` | Admin; shared disk policy, per-run cap and only proven-free clone candidates. No unknown node or missing observation becomes an empty/healthy result. |
| `mop server sweep [--dry]`, node `junk/sweep`; `mop driver sweep` | `maintenance.sweep.preview/run`; local node watchdog remains a node command | Admin/internal node service. Distinguish orphan bodies from ambiguous build bodies. No authoritative job list means refuse destructive sweep; no replies means unexamined. Preserve dry reports, per-node failures and disk watchdog thresholds. |
| `mop server disk [node]` | `nodes.disk.retrieve` | Admin; filesystem free/total is a shared-machine fact, not a project role privilege. |
| `mop setup`, `mop server setup`, `mop login`; server deploy/config/user setup | Local operator dependency setup, Django client login/configuration, local controller bootstrap/deploy and initial administrator recovery commands | Local operator for machine changes; Django authentication for online login/session. Initial setup must work before a usable HTTP service exists. No compatibility promise for connection.json, legacy credential pairs or file-user imports. Authentication-provider and administration semantics belong to API.md. Never make local bootstrap depend on being logged into the service it is creating. |
| `mop server cluster/bootstrap/callout/web`, `mop agent`; `mop driver build/run/exec/clone-work/purge-clone/list` | ASGI/application workers and authenticated node execution services; local driver/install commands where machine-local | Service/Local operator, not an arbitrary remote `exec` endpoint. Preserve build/runtime/bootstrap/cleanup responsibilities, not obsolete NATS subscriber daemons as public CRUD APIs. Internal-only startup and identity functions remain internal-only. |
| `mop server proxykey start/status/resume`; fixed-path key delivery | Explicit Admin proxy-key rotation operation/status/resume, or controller-local recovery command per API.md | Exclusive writer, complete recipient set (including offline nodes), no secret in result. Preserve partial/unknown and resumable delivery state; failed observation is not safe to repeat blindly. |
| `/llm/management.html`, `/llm/raw/management.html`, `/v8/management/*` | Protected proxy management gateway behind Django current Admin authorization | Server-held management key, fixed trusted loopback upstream, bounded/allowlisted paths, methods, redirects and response headers. Cookie writes require CSRF/Origin; strip client authorization/cookies before upstream use, redact credential-bearing configuration. Provider failure/revocation closes access. No public management pass-through or client-selected upstream. |
| Public inference proxy, `/v1/models` and inference requests; `mop models` | Separate inference gateway and authorized model-catalog endpoint | Inference client credential and provider quota policy, not dashboard Admin cookie or proxy management key. “Public” means externally reachable inference, not anonymous control access. Do not expose management through the inference route, or require pool management authority merely to use inference. |
| `mop dev bug/ci/docs/test/web` | Developer-local toolchain, outside pool control API | Existing tracker/CI/development authority; no new remote code execution endpoint. Tests and build commands are not runtime pool operations. |

## Result and safety requirements

API.md defines the wire vocabulary. Every frontend must distinguish admission,
started effect, observed completion, refusal, failure, partial result and unknown
outcome. A timeout does not prove no effect occurred. Operation retrieval and
realtime receipts use the same canonical record; private content does not leak
into a public version of that record. Build logs and operation progress need
bounded authorized retrieval as well as live notification. Do not claim exactly
once delivery or crash recovery merely because Channels or a task runner exists;
STORAGE.md defines reconciliation and durable records.

The replacement must preserve clone/lease gates at the point of mutation,
including concurrent send/dispatch/identity updates and creation versus project
limits. A generic CRUD update must not bypass them. Roles are pinned capabilities,
not a name-to-admin conversion. Host driver occupants with a shared OS identity
are not mutually isolated; endpoint authorization does not manufacture an OS
sandbox. Internal node credentials stay node-scoped and never grant controller
administration or scheduler-token access.

## Source evidence and remaining acceptance gaps

This inventory was read from the pre-Django worktree, not exercised on a pool:

* `mop/cli/project/{add,delete,limit,list}.py`, `mop/server/builder.py`
  (`needs_build`, `run`), `mop/server/image.py` (`prepare`, `plan_clear`,
  `clear`, `build`): build modes, verified branch, selected node and force recovery.
* `mop/server/cluster.py` (`refusal`, `VERBS`, `_project_*`, `_secret_*`,
  `_landing`, grant authorization); `mop/cli/node/*.py`: registry, machine
  authority, project deletion/limits, secrets and node lifecycle.
* `mop/cli/core/{add,update,stop,attach,code,_launch}.py`,
  `mop/common/ops.py`, `mop/common/soul.py`, `mop/server/grants.py`,
  `mop/server/bootstrap.py`: role-only creation, incarnation, approved policy,
  spawn/resolve, registration reconciliation and allowlisted secret staging.
* `mop/cli/service/mcp.py` (`agents`, `_masters`, `send`, `dispatch`, `slash`,
  `on_inbox`), `mop/client/channel.py`, `mop/node/agent.py`: local/live addressing,
  human versus grant delivery, dispatch transaction and idle/report receipts.
* `mop/cli/session/{history,terminal}.py`, `mop/server/terminal.py`,
  `mop/node/terminal.py`, `mop/cli/server/web.py`, `mop/server/websession.py`:
  private history, actual PTY attach, browser binding, epochs and input fences.
* `mop/cli/core/stat.py`, `mop/usage.py`, `mop/cli/server/{doctor,gc,sweep,disk}.py`:
  usage attribution, safe treatments, unknown-state safeguards and local maintenance.
* `mop/cli/server/web.py` (`Gateway`, `llm_api`, `_llm_admin`),
  `mop/server/proxyapi.py`, `mop/server/proxykey.py`,
  `deploy/roles/proxy/templates/`: protected management versus inference routing.

Implementation acceptance still requires finite serializers and internal service
endpoint enumeration for these bindings, permission registration/mapping, explicit
presence/receipt retention, and measured cross-worker terminal reservation and
cleanup. Public projection is opt-in; local repair, execution reconciliation,
secret visibility and authority follow API.md, not a frontend-specific variant.
Where a delegated old verb maps to a new action, implementation must record that
finite mapping and test denied capabilities as well as allowed ones. These are
named requirements, not implemented guarantees.
No Django endpoints, migrations, Channels consumers or tests are supplied by
this documentation change. Runtime, security, failure/recovery and live-pool
verification belong to the implementation tasks; source reading is not a green
runtime check.
