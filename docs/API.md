# New controller contract

This is the normative contract for the new installation in epic #623, established
by #624. It is a specification, not a claim that the current controller implements
Django or these endpoints. The implementation starts with #632. Existing subsystem
documents describe the deployed version unless explicitly marked otherwise.

The new controller is a modular Django monolith. Django ORM owns persistent mop
state, PostgreSQL is the production database, Django Auth owns human authentication,
DRF serves commands and queries, and Channels/ASGI serves realtime. NATS is an
internal realtime transport, not an alternative management API or durable queue.

See [STORAGE](STORAGE.md) for the reader/writer and deletion inventory,
[CONTROL](CONTROL.md) for the functional/permission inventory, and
[CONFIG](CONFIG.md) for the installation boundary. The epic's dependency table
remains the delivery order; the module dependencies below are import rules.

## Release and compatibility boundary

The release starts with an empty database and new users, nodes and puppets. Normal
Django migrations create and subsequently evolve that schema. There is no import
of old files/users/password hashes/cookies/puppets, file fallback, dual-write,
compatibility endpoint, management NATS RPC or mixed-agent rollout. External Git,
Nomad and model-provider state is not silently adopted as mop authority.

A working old pool is not a migration test fixture. All integration and fault
injection below use a new isolated installation. Nothing here authorizes deleting
an installation, credentials or data. The epic integrates only into
`epic/623-unified-pool-api`; merging to `master` and changing the automatic deploy
require a separate operator decision before any incompatible release can ship.

## Supported foundation

| Component | Contract |
|---|---|
| Python | 3.12 and 3.13; clients and nodes do not acquire server dependencies |
| Django | 5.2 LTS, maintained security patch selected and declared by #632 |
| DRF | 3.16 compatible maintained patch; synchronous dispatch/services |
| Channels | 4.x compatible maintained patch, selected and checked in #632 |
| ASGI server | Maintained compatible production server, declared in #632; not `runserver` |
| Database | PostgreSQL 16 and 17; mandatory CI on both for the new controller |
| Optional backend | SQLite may run fast pure/schema checks; not a supported controller runtime or concurrency gate |

These are support decisions, not claims that every backend Django supports has
been tested. Adding a backend requires the same migration, transaction, locking,
constraint, recovery and backup gate before advertising support. Unsupported
capabilities fail system checks, rather than weakening a domain rule.

Use portable Django fields, relations, constraints, conditional updates and
transaction/row-lock APIs. No vendor SQL, PostgreSQL-only field/extension, advisory
lock, PRAGMA, connection-vendor branch in business rules, or SQLite file locking.
Backend provisioning, driver, TLS/options, pooling and backup tooling belong to
the infrastructure boundary. Do not wrap QuerySet in a generic repository or
introduce a second unit-of-work framework.

PostgreSQL uses read committed isolation. Unique constraints establish identity;
updates use expected revisions; row locks coordinate a bounded set of resources.
Acquire project sequence rows by project ID, then resource rows by `(kind, id)`,
then aggregate rows by `(kind, id)`, consistently across handlers. Locking new
resources is protected by their unique identity constraints; competing creation
retries the short transaction after re-reading. No transaction contains external
I/O or waits for an effect. No global `ATOMIC_REQUESTS` for network actions.

Initial safety limits for #632 are a 5 s connect timeout, 2 s lock timeout, 5 s
statement timeout and 10 s maximum application transaction duration. They are
infrastructure settings, not hard-coded installation values. A timed-out
transaction rolls back; an uncertain COMMIT needs lookup/reconciliation, not an
assumed rollback. Database connection limits and worker bounds are provisioned
from the measured profile below, with explicit headroom for migrations/repair.

## Modules, imports and public boundaries

All ORM code lives below `mop/server/`. Common policies/value types remain
framework-free; `client`, `node`, drivers and stdlib-only `session.py` do not import
Django. Model names here are contracts, not a requirement to create one app per
model. A module exposes explicit commands/queries; consumers do not update its
private tables.

| Module | Owns | Public use cases/queries | May depend on |
|---|---|---|---|
| Catalog | Project, Node, settings, image desired/observed metadata, client binding | configure project/node, resolve scope, list configured targets | Common values/policies |
| Access | Principal, membership, RoleBundle, Grant, creation intent; Django Auth adapters | authorize action, resolve actor, approve bundle, issue/revoke grant | Catalog; secret references through secrets public interface |
| Secrets | encrypted SecretVersion, CredentialRotation, delivery receipts | stage/activate/revoke rotation, authorize addressed delivery, read metadata | Catalog; trusted actor value and an injected access decision, not access ORM imports |
| Preparation | bootstrap revision/approval/result, build input snapshot | pin/approve preparation, inspect build inputs and receipts | Catalog, Access, Secrets |
| Operations | Operation, Step, claim, resource coordination, audit and outbox | admit command, claim step, record result, reconcile, query status | Catalog, Access, Secrets, Preparation; narrow external ports |
| Sessions/delivery | human Assignment, incarnation, observations, endpoint, delivery, TerminalControl, usage | resolve assignment/facts, register endpoint, ingest telemetry, claim terminal control, deliver report | Catalog, Access; operation ID values, not operations private models |
| Composition root | handlers, adapters, worker and gateway lifecycle | bind explicit handlers/ports, start/stop runtime | All public interfaces |

Operations call session/assignment queries and commands through injected narrow
interfaces at the composition root. Sessions/delivery does not call operation
admission recursively. Secrets receives an authorization decision made in the
same application transaction where necessary, rather than importing Access
models and creating an Access–Secrets import cycle. Transactional composition is
an application responsibility, not an excuse to make each module an HTTP service.
The #632 architecture check enforces this directed dependency graph, including
TYPE_CHECKING and dynamic imports; a dependency injection callback must not hide
an arbitrary cross-module writer.

DRF/Admin/WS adapt input and trusted authentication, then call the same use case.
Use cases take typed immutable values/ActorContext, not HttpRequest or ASGI scope.
Pure policies may be functions. A handler may be a function; a class is justified
by cohesive state/dependencies. Do not create a God PoolManager/OperationService,
role inheritance tree, universal lease, JSON workflow DSL, arbitrary-import
handler registry, mandatory BaseOperation, business effects in save/signals,
Event Sourcing, or duplicate entity/DTO for every ORM model. Return materialized
snapshots, not live QuerySets, deferred ORM instances or shared mutable lists.

## Invariant ownership

The owner column names the single rule owner, not an exclusive file. Structural
constraints accompany each rule; validation on an in-memory model alone does not
protect a concurrent update.

| Invariant | Rule owner | Only mutation path | Transaction boundary | Required check |
|---|---|---|---|---|
| Project identity derives from origin via `driver.project_of` | Catalog policy | configure project | unique identity + expected revision | collision, renamed origin, forged project FK |
| RoleBundle bytes/digest are immutable | RoleBundle policy | approve/pin bundle | create unique digest/revision; immutable after approval | changed bytes, partial role triple, concurrent approval |
| Grant cannot amplify owner permissions or approved spawn policy | Access delegation policy and Grant lifecycle | issue/delegate/revoke grant | current owner rights, bundle/incarnation, parent chain and CAS checked together | revoked ancestor/owner, changed target, stale incarnation, disallowed spawn |
| Human ownership protects a clean freshly dispatched clone and unfinished work | Assignment policy | dispatch/reassign/release | assignment revision + fresh clone observation + target incarnation | concurrent dispatch, dirty/unknown clone, force audit |
| One executor owns a resource while an effect might still be running | ExecutionClaim policy | claim/renew/reconcile step | ordered resource rows + claim generation + CAS | timeout with late effect, duplicate/expired claim |
| Operation transitions and request identity do not diverge | Operations lifecycle | admit/record/reconcile | request uniqueness + aggregate CAS + result/audit/outbox | duplicate key, conflicting payload, lost COMMIT reply |
| CredentialRotation activation requires addressed delivery evidence | Rotation policy | stage/deliver/activate/revoke/reconcile | rotation CAS + receipts + associated outbox | crash per stage, stale receiver, conflicting compensation |
| TerminalControl has one live writer bound to browser session and incarnation | Terminal policy | acquire/renew/release control | unique target control + generation/CAS | competing tabs, session logout, dead owner, new incarnation |
| Bootstrap approval authorizes exact bytes and role/secret references | Preparation policy | publish/approve/record preparation | immutable snapshot + revision + addressed receipt | changed task bytes, revoked approval during I/O |
| Entity revision, projection change and event are consistent | Owning aggregate + Operations publication contract | aggregate use case | same transaction with project sequence + outbox | rollback, duplicate publish, stale snapshot |
| An endpoint/report is addressed to a live authorized session | Delivery policy | register/renew endpoint, deliver/acknowledge | bound credential/session + incarnation + receipt identity | forged address, stale endpoint, duplicate receipt |

Different kinds of lease are not interchangeable: human Assignment guards work;
ExecutionClaim authorizes an external effect; TerminalControl authorizes bytes to
a particular live PTY. Expiry of one never grants another.

## Authentication, scope and revocation

Use standard Django User, Group, Permission, password hashers and database-backed
sessions. Principal/membership/grant are related domain records, not subclasses
of User. `is_staff` grants Admin login eligibility only. `is_superuser` is explicit
global access, but still cannot bypass target identity, fencing or effect safety.

Authorization is current action permission plus server-resolved project
membership plus operation-specific ownership/delegation constraints. An explicit
all-project membership is the global project scope; a client-supplied project,
owner, actor, node, `_caller`, inbox or grant ID is never authority. Resolve each
object's project from its actual record/origin, including nested FKs and M2M.

Use the permission vocabulary in [CONTROL](CONTROL.md), registered as ordinary
Django permissions in the owning app; standard add/change/delete/view permissions
apply to configuration only where that document permits. One scope helper applies
to list filtering, detail lookup, creation, FKs/M2M, Admin actions/inlines, worker,
artifact/history reads and WS subscriptions. Unauthorized detail/cross-project
references return 404; an authenticated in-scope actor missing an action gets 403.
A configuration model permission never grants a physical action.

Browser authentication uses Django login/logout and SessionAuthentication with
CSRF, including explicit protection of login. Unsafe session-authenticated HTTP
requests require CSRF; login has rate limits and secure HttpOnly SameSite cookies.
CLI/MCP use standard DRF TokenAuthentication over HTTPS. There is one token per
User, no built-in expiry and no per-device sessions: do not promise either.
Standard DRF's token model stores the bearer key rather than a non-reversible
verifier. This framework-owned credential is an explicit exception to the
application SecretVersion AEAD scheme; do not claim standard DRF provides hashed
or AEAD token storage. Restrict database/backup access and never expose token rows
in Admin/audit/general APIs. Changing that representation requires a separately
reviewed authentication decision, not an undocumented custom token framework.
Rotation deletes/replaces the token atomically; one rotation invalidates all
clients using it. Separate service users isolate machines/devices where required;
they are non-staff/non-superuser with unusable passwords and minimal permissions.
A service principal cannot select a human owner in JSON.

First-client credential issuance is `POST /api/v1/auth/token`: authenticate the
username/password using the authoritative Django backend over HTTPS, rate-limit
by account/source, and return the existing standard user token only in this
explicit no-store credential response. Do not log credentials or response body.
This password-authenticated endpoint is not a session-authenticated mutation;
browser issuance instead uses a CSRF-protected logged-in credential action.
Machines receive their service-user token through audited local enrollment, never
by enabling password login for that service user. `POST /api/v1/auth/token/rotate`
and `POST /api/v1/auth/token/revoke` require the current token or a reauthenticated
CSRF-protected session of that user (or explicitly authorized user administration).
Revoke is idempotent and reveals no token. CLI logout revokes the shared token,
invalidating all clients of that user; merely dropping transient local bootstrap
material is disconnect, not logout. No per-device logout is promised by the
one-token model. Initial address/CA trust and the first credential response are
the narrow pre-login bootstrap described in CONFIG, not a new local user registry.

If LDAP is enabled it is a Django auth backend with authoritative login routing:
the first authoritative source for a known login decides, including wrong
password, ambiguous account or denied group. No fallback to another source after
that denial. Direct/nested AD group mapping, all-project scope and LDAP-owned
membership revocation have explicit checks; a backend cannot overwrite local
identity or store LDAP passwords.

ActorContext contains credential-bound actor/principal kind, human owner where
applicable, scope and grant identity. It is constructed at a trusted boundary,
not from the payload or a module-global current user. It is a reference for live
authorization, not a permanent capability snapshot.

| Change | New requests, pending/retry steps and secret issuance | Existing WS/terminal/delivery | Already running process |
|---|---|---|---|
| Browser logout | session invalid immediately; token independent | close connections/control bound to that session | do not kill puppet |
| Token revoke/rotate | old token rejected; browser independent | close token-bound endpoints; browser session unaffected | do not kill puppet |
| User deactivation | all actor paths rejected | close all bound streams/control/endpoints | do not kill puppet |
| Permission or membership removal | re-resolve current scope before start/retry/issuance | recheck before each protected delivery and after waits; close/filter lost scope | no automatic destructive rollback |
| Grant/ancestor revoke or owner rights loss | descendants cease to authorize start/retry/delivery | close grant-bound subscriptions/control | reconcile any in-flight effect; no new stage |

A durable revocation event wakes consumers, but correctness does not rely on its
arrival: check before each protected frame/byte delivery, renewal, credential
issuance and after every await that can change authority. Consumers check idle
connections at most 5 s apart. Perform admission and execution-time checks from
current state in their transaction; each external stage revalidates immediately
before dispatch. Revocation racing an already authorized external call cannot
recall bytes already sent; record in-flight outcome and deny later stages. A
read-after-wait reauthorizes before returning data. No secret or private transcript
is broadcast while waiting for a best-effort revocation notification.

## Identity and wire versioning

* `schema_version` is the wire schema major, initially 1, not a row revision.
* `id` is an opaque stable resource ID; a name may be reused, an ID may not.
* `incarnation` is a fresh opaque ID for a newly created runtime/session identity.
* `revision` is a monotonically increasing integer on that entity, encoded as a
  decimal string to avoid JavaScript precision loss. Zero is not unknown.
* `digest` identifies canonical immutable bytes with SHA-256, not authority.
* `epoch` identifies the database/projection timeline and changes after restore
  or destructive rebuild. It is not a deployment number.
* `cursor` is an opaque authenticated event/history position bound to epoch,
  project scope and authorization generation. It is not an entity revision.

IDs/incarnations/revisions/digests get distinct value types where confusion could
mutate the wrong object. Unknown observations use explicit freshness/reason, not a
fake empty clone. Times are UTC RFC 3339; deadlines use the controller clock and
clock uncertainty rejects unsafe action. Names/origins are display/lookup inputs,
not a substitute for identity.

## REST commands and queries

All new public resources live under `/api/v1/`; new realtime under `/ws/v1/`.
HTTPS is mandatory outside a test loopback fixture. Standard DRF authentication
failures may be 401 for TokenAuthentication or 403 for SessionAuthentication;
clients use the machine-readable auth code, not a fabricated universal 401 rule.
Responses carrying user/private data use `Cache-Control: no-store`.

Each command has a finite typed schema/handler and an explicit endpoint. A common
admission/lifecycle implementation does not expose a generic arbitrary RPC,
subject, shell/argv/cwd/env, URL or Python callback field. Unknown fields fail
validation. Commands name exact resource identity/incarnation, expected revision,
client-generated UUID `request_id`, expected `epoch` and `not_after`. Creation
commands instead bind an authorized parent/project and its expected revision;
admission reserves the new resource identity/incarnation in the operation before
execution. Replacements bind the expected source identity and separately record
the resulting incarnation. Receipts name both bindings and their operation/claim,
so a legitimately new runtime is not confused with an unrelated replacement.
A deadline is required and cannot be extended by retrying the same request. The maximum admission horizon is
24 h; destructive commands use at most 5 min and are rechecked at dispatch.

Example (illustrative IDs):

```http
POST /api/v1/puppets/p_123/commands/restart
Authorization: Token <redacted>
Content-Type: application/json

{"schema_version":1,"epoch":"e_1","request_id":"8e6449b9-65db-4148-aa68-a8b4020bd8b5","incarnation":"i_456","expected_revision":"12","not_after":"2026-10-16T10:05:00Z","params":{"fresh":false}}
```

```http
HTTP/1.1 202 Accepted
Location: /api/v1/operations/o_789
Cache-Control: no-store

{"schema_version":1,"epoch":"e_1","operation":{"id":"o_789","request_id":"8e6449b9-65db-4148-aa68-a8b4020bd8b5","state":"accepted","revision":"1","target":{"id":"p_123","incarnation":"i_456"}}}
```

202 is emitted only after admission COMMIT, never as proof of execution. Lost
response/connection after possible COMMIT is unknown to the client. Query
`GET /api/v1/operations?request_id=<uuid>` in the same actor scope before deciding
anything; retry the exact envelope with the same key, not a new physical command.
The same actor + key + canonical validated command returns the same operation;
a different target/payload/deadline with that key returns 409 `request_conflict`.
Lookup order is normative: authenticate, reject a stale installation epoch and
authorize disclosure, then find the actor/request identity and compare canonical
intent. An identical existing request returns that operation even if its original
deadline expired or target revision/incarnation has since changed. It does not
reauthorize execution. Only an unseen request undergoes new-admission deadline,
revision, incarnation and ownership checks. Execution always rechecks current
preconditions. Current scope still controls disclosure of an old result.
Canonicalization includes epoch, action, source/parent identity, any explicitly
requested result identity, incarnation, expected revision, deadline and validated
parameters. A server-reserved result binding is stored once with admission, not
regenerated when comparing a retry. Secret content is represented by its bound
immutable secret version/digest, not logged plaintext. An admission unique
constraint resolves concurrent duplicates, not process-local dictionaries.

Keep operations at least 90 days after completion. Keep a minimal request
identity/digest tombstone after result expiry: an expired key returns 410
`result_expired`, never a new effect. Requests still pending cannot be pruned.
Scope/privacy erasure and tombstone retention must be resolved explicitly by the
operator's retention policy before deployment, without allowing old-key replay.

`GET /api/v1/operations/o_789` returns state, safe reason/code, target, revision,
confirmation evidence metadata and whether resources remain held. It never
returns message text, terminal bytes, credential, environment or raw exception.
Query endpoints and read models do not secretly repair or launch effects.
Pagination uses opaque scoped cursors with bounded page size (default 100,
maximum 500); invalid/expired cursors return a reset instruction, never an empty
success that silently drops history.

Errors have a stable shape:

```json
{"schema_version":1,"error":{"code":"revision_conflict","message":"target changed; refresh before submitting","retryable":false},"epoch":"e_1"}
```

400 validation; 403 auth/permission as above; 404 absent/out-of-scope; 409 revision,
incarnation/resource conflict; 410 expired result/cursor with reset metadata; 429
bounded admission limit with Retry-After; 503 unavailable authority/database.
Preserve safe library refusal reasons. Raw untrusted exceptions are translated
once at their adapter, not repeatedly sanitized into an empty generic error.
A failed HTTP response is not proof the external effect failed.

## Operation, claim and compensation lifecycle

| From | To | Evidence/precondition |
|---|---|---|
| absent | accepted | current auth/scope, typed command, unique key; commit request/audit/outbox |
| accepted | running | current auth/ownership/deadline/incarnation; acquire resource claims |
| accepted | refused / expired / cancelled | explicit denial, expired unstarted command, or permitted cancellation; no effect |
| running | succeeded | effect-specific confirmation bound to the admitted source and explicitly recorded resulting incarnation |
| running | refused / failed | confirmed refusal or confirmed failure with known effect boundary |
| running | partial | confirmed subset and explicit remaining/recovery information |
| running | reconciling | crash, uncertain effect/COMMIT, loss of executor or cancellation after possible start |
| reconciling | succeeded / failed / partial / unknown | confirmed observation, or inability to establish outcome |
| unknown | reconciling | explicit authorized reconciliation; never blind effect re-execution |

State updates require expected revision and persist result/audit/outbox together.
Cancellation before start can cancel; after possible start it requests bounded
cooperative interruption/reconciliation and does not assert rollback. A client
wait timeout changes no execution rights or outcome. `unknown` is a displayable
outcome, not evidence that a resource is safe to release. Claims remain fenced
until confirmed effect completion/absence or explicit safe operator recovery.
A new claim generation cannot make an old physical effect disappear. For adapters
without enforceable fencing, recovery must prove the old executor is quiescent;
otherwise deny a conflicting effect. Late receipts are checked against step,
claim generation and incarnation; they cannot overwrite a newer decision.

OperationStep records explicit confirmed stages, not a workflow scripting
language. Concrete restart/build/rotate handlers define confirmation and recovery.
A send payload needed after acceptance is stored encrypted with a purpose-bound
secret reference, expiry and recipient; purge after terminal delivery/expiry.
General results/audit/outbox contain only safe metadata. A digest alone cannot
recover a message; if its required bytes are lost, reconcile/unknown, not a
fabricated successful retry.

Grant preparation reserves request identity and a pinned bundle without granting
runtime authority: `reserved -> active` requires the matching creation/binding
receipt, or `reserved -> revoked/expired` terminates an unactivated reservation.
An active grant transitions only to `revoked/expired`; issuing a delegated child
checks the active ancestor chain, owner rights and pinned approved policy.
Logical revocation denies new acts immediately; transport-credential retirement
is a separately recorded fallible stage, never a reason to keep logical access.
Revocation never reactivates an old grant. Role edits create a new immutable
bundle and require a new approval; they do not change existing digest bytes.

Rotation transitions are `staged -> delivering -> active -> retiring -> retired`,
with `reconciling` on uncertain delivery/revoke and `failed` only on a known safe
failure. Activation requires the configured addressed receipts; old credentials
are not retired merely because a send timed out. Build records pinned inputs,
affected bodies/resources, per-stage receipts and known prior state before any
removal. Recovery checks current revision/incarnation before restoring anything.
Compensation is a new fallible effect; retain both original and compensation
errors. There is no universal undo or guarantee of SQL-like rollback of Nomad,
provider credentials or a filesystem.

## Narrow external ports

| Port | Contract | Required fake/real contract cases |
|---|---|---|
| Scheduler | inspect/apply named desired job revision with external receipt | absent, refused, committed-but-response-lost, replacement incarnation |
| NodeExecutor | claim-bound named step on assigned node/incarnation, result/inspection | stale fence, duplicate receipt, still-running timeout, node restart |
| ImageBuilder | pinned build inputs, declared resource set, stage receipts/reconcile | failure before/after body removal, conflicting newer build, compensation failure |
| ProxyManagement | scoped catalog/key/rotation/gateway actions, no credential in general result | provider refusal, uncertain create/revoke, redaction, upstream timeout |
| EventPublisher | publish immutable outbox event ID; duplicates allowed | disconnect before/after publish, duplicate, bounded backlog |
| TerminalStream | private live PTY bytes/control with explicit owner/cleanup | writer conflict, logout, disconnect, cancellation, no replay of input |

Each consumer defines the narrow interface it needs; no all-powerful
`Transport.call(service, verb, payload)`. Binding adapters, clock, IDs and limits
is the composition root's job. Real adapters and fakes share these behavioral
checks; fake I/O does not substitute for PostgreSQL transaction tests.

## Machine execution protocol v1 (#639 / #640)

The server implementation is `mop.server.sessions` (Django app label
`execution`, distinct from Django's own `sessions`). `protocol.py` is an
immutable, framework-free wire contract. Node code consumes JSON; it must not
import Django or server modules. These routes are not a generic RPC interface.

- `GET /api/v1/machine/work?schema_version=1&node_id=<uuid>&executor_epoch=<uuid>&limit=10`
  returns at most 50 existing Operation/Step projections. It never creates work,
  claims a resource, or repairs unknown outcomes. Empty work is distinct from
  unavailable authority (503). Polls are immediate; clients bound cadence and I/O.
- `POST /api/v1/machine/work/claim` accepts exactly `schema_version`, `request_id`
  and `work`. `work` contains exactly `schema_version`, `epoch`, `operation_id`,
  `step_id`, `target_id`, `incarnation_id`, `node_id`, `executor_epoch`, `owner_id`,
  `generation`, `revision`, `role`, `bundle_digest`, `grant_id`, `action`,
  `not_after`. UUIDs are canonical strings, positive counters and owner ID are
  decimal strings, deadline is an aware ISO-8601 timestamp. Schema version is
  the integer 1. The current admitted projection must match every field; caller
  values cannot choose authority. Initial actions are `restart_session`,
  `apply_identity`, `send_message`, `confirm_ready`; no shell, command, path,
  callback, hypervisor build or arbitrary parameters are accepted.
- `POST /api/v1/machine/work/result` accepts exactly `schema_version`,
  `attempt_id`, `expected_revision`, `receipt_id`, `outcome`, `evidence`.
  Outcomes are `succeeded`, `failed`, `unknown`. Evidence is a bounded public code,
  never subprocess output, credentials or a proof fabricated from a timeout.
- `POST /api/v1/machine/executor/restarted` accepts `schema_version`, `node_id`,
  `previous_epoch`, `new_epoch`. This fences old process admissions and marks
  unresolved attempts unknown; it does not authorize replay or claim that the
  old process is quiescent.
- `GET /api/v1/machine/facts` accepts `schema_version`, `node_id`, `kind`, `limit`.
  Allowed kinds are `states`, `clone`, `sizes`, `usage`, `disk`, `junk`, `health`;
  a bounded fact port returns explicit freshness/unavailability, never a mutation
  Operation. Missing bindings refuse; unavailable is not an empty safe-to-delete
  inventory. Expensive I/O must have adapter-level deadlines outside transactions.

`Assignment` owns human ownership, project/node, role/digest/grant and generation.
Creation reserves a fresh target identity; it never assigns an existing clone.
Existing-target `reassign` requires an explicitly released human assignment,
a fresh addressed clean-clone observation and no unresolved effect, then rechecks
revision, incarnation, authorization and fact age under the assignment lock.
An unresolved effect is checked before requesting a fresh clone observation and
again under the assignment lock: a clean observation made before old-effect
quiescence is not permission to reassign. The clone query must perform a fresh
addressed read, not return a previously cached clean observation.
Lease expiry alone does not establish a clean clone. Human target scope is its
actual project, not every unrelated project hosted on the same physical node.
`Assignments.current(project_id, target_id)` is the public internal binding query
for Access/Secrets composition. It returns an immutable `AssignmentBinding`
(target/project/node/incarnation/process epoch, owner, role/digest/grant,
generation/revision and matching identity/readiness confirmations), or none for
an unbound, released, expired or retired source. It is not an authorization or a
credential response; consumers must recheck current access and revision before
issuing bytes/admission and after waiting. They need not import sessions models.
`Assignments.activate` accepts operation-reserved `incarnation_id` and `grant_id`
UUID values. A handler must pass its stored result binding rather than generating
a different identity when completing or recovering an admitted operation.
`ExecutionIncarnation` binds a fresh opaque incarnation to a node process epoch.
`AssignmentAttempt` is a resource fence and receipt for an already-admitted step,
not a pending-work queue. Operation and Step links are UUID values, never imports
of private operations models. A unique operation/step and a conditional unique
unresolved assignment enforce admission across processes. `LandingToken` guards
human landing ownership only; TerminalControl remains a separate realtime domain.
`IdentityApplication` and `ReadinessReceipt` require matching confirmed attempts.
Send admission requires identity confirmation for that exact assignment generation,
incarnation, role, digest and grant. None of these rows grants terminal control.

`MachineExecution` receives narrow ports and a current installation-epoch query
from the composition root. The infrastructure restore guard is locked before
subject rows for claim/result; disabled effects refuse new claims, not evidence
inspection:

| Port | Preconditions / confirmed return | Failure and transaction rule |
|---|---|---|
| WorkPort v1 | `pending(node_id, executor_epoch, project_ids, limit)` filters current authorized project IDs before applying the bound, and returns immutable WorkV1 projections; `current(operation_id, step_id)` rechecks initiating actor/operation authority; `claimed(work, attempt_id)` records existing step admission; `completed_result(work, attempt_id, outcome, evidence)` records its result | No independent queue, no external I/O. Current/claimed/result participate in caller's short atomic transaction. Idempotent by attempt ID; an unknown commit is reconciled, not replayed. |
| GrantAuthority | `authorize(owner_id, project_id, target_id, grant_id, role, bundle_digest, incarnation_id, action)` rechecks active approved policy, current owner rights and complete ancestor chain | Deny by default. Production binding must use Access's public grant query; a fake is only a test fixture, not production authority. |
| FactReader | `read(node_id, kind, limit)` returns a bounded versioned freshness-bearing projection | No mutation or repair; bounded I/O outside DB transactions; explicit unavailable, never false empty success. Target/project/incarnation scope and live credential are checked again after I/O. |
| CleanCloneQuery | `observe(target_id, incarnation_id, node_id)` returns an addressed `FactV1` | Read only outside a surrounding transaction; unknown/dirty/stale or a changed source refuses reassignment. Default unbound. |
| EffectInspector | `inspect(work, attempt_id)` returns typed `InspectionV1` bound to attempt, source incarnation and executor epoch, with known outcome/evidence and verified quiescence | Read only outside any transaction; no replay/compensation. `reconcile` requires an explicitly authorized human, repeats authorization/CAS after inspection and closes only the old fence. No client-supplied `quiescent` boolean is accepted. |

`WorkPort.current` returns the pinned admitted intent for a still-active claimed
step too: a lost claim reply must not be rejected merely because the step is now
running. It rechecks execution authority but does not claim or enqueue anything.
`claimed` and `completed_result` must identify the existing step by operation/step
values and the same durable attempt ID, and write their state/audit/outbox in the
caller transaction.

Lock order inside sessions is Assignment then incarnation/attempt. Work port
implementations must not acquire operations locks in the reverse order on another
path. Claim admission refuses a surrounding transaction: releasing a nested
savepoint is not the durable commit required before returning an execution permit.
The injected work callbacks join the claim-owned transaction; they do not wrap it
in another outer transaction. No callback may perform network I/O while these
rows are locked. Executor restart advances the assignment revision as well as
retiring its process binding, so pre-restart activation CAS cannot overwrite the
newer facts. A duplicate restart report recognizes an already-activated resulting
process and never retires that newer incarnation. Read-only
poll results are hints; all gates run again on claim. Auth uses the actual Access
service (live credential, action Permission, membership and enrolled node). Lease
expiry and executor restart retain the unresolved resource fence. Explicit,
authorized reconciliation with effect-specific quiescence/confirmation evidence
is required to resolve unknown work. Historical pre-restore work remains fenced
from admission/results, but a human can reconcile its old fence under a stable
current installation epoch, including while new effects are disabled. This
records historical evidence only; it does not revive old-epoch work. The recovery
epoch is rechecked after inspection. No automatic second attempt is created.
An identical claim retry can recover a still-valid admission; it cannot revive an
expired, unknown, revoked or old-incarnation permission. Receipt retries return
stored evidence without invoking a second effect. Changed duplicate receipts or
old-generation rollback/results are conflicts, never a write to newer ownership.
The first vertical adapter to #625 and the controlled new-node restart with #640
are separate integration acceptance, not implied by a service/fake test.

## Sync/async and resource lifecycle

DRF admission/query, claim and result use short synchronous ORM services.
A worker independent of the HTTP request performs external effects after commit.
ASGI/Channels, NATS, PTY, HTTP node I/O and the agent remain asynchronous.
Blocking Git/Ansible/provider libraries run in bounded executors/processes, not
on an event loop and not under a database lock. Do not rewrite all pure functions
as async or put raw async handlers on synchronous DRF dispatch.

An async caller invokes the whole synchronous transaction with
`database_sync_to_async` or `sync_to_async(thread_sensitive=True)`. It does not
wrap individual ORM lines, pass a connection/transaction across threads or return
a lazy QuerySet. `DJANGO_ALLOW_ASYNC_UNSAFE` is forbidden. ASGI database access
uses `CONN_MAX_AGE=0`; any pooling is bounded and backend-aware. HTTP requests,
workers and long-lived consumers close obsolete connections; consumers call
`aclose_old_connections` when waking after inactivity, not just on disconnect.

#632 starts with at most 8 simultaneous external effects and 8 bounded blocking
jobs per controller instance, 4 concurrent effects per node, 64 queued commands
per project and 256 per instance. A resource can have only one unresolved
conflicting effect regardless of instance count. Enforce admission/claim bounds
in shared state, not a local semaphore alone. Size a pool at no more than 24 DB
connections per application instance initially and retain at least 8 server-wide
connections for administration/tests; deployment validates the total across
processes against DB capacity. Exhaustion gives backpressure, never unbounded
threads or connections. Installation settings may lower these caps.

Each subscription, executor and connection has a startup/shutdown owner. Shutdown
stops admission/claims, drains bounded tasks, closes PTYs/subscriptions/HTTP/NATS
clients and DB connections; unconfirmed steps stay durable for reconciliation.
Async cancellation releases local resources in `finally` but never marks a
possibly running external effect complete merely to free a slot.

Framework references: [Django async](https://docs.djangoproject.com/en/5.2/topics/async/),
[DRF releases](https://www.django-rest-framework.org/community/release-notes/),
[Channels database lifecycle](https://channels.readthedocs.io/en/stable/topics/databases.html).
Exact patched dependencies and reproducible install belong to #632, not this
prose; these upstream pages do not imply that mop has passed the new tests.

## Outbox, snapshot and event convergence

Aggregate change, affected project sequence, safe audit and immutable outbox row
commit in one transaction. A per-project sequence row is locked while assigning
the position and applying its changes; transactions involving multiple projects
lock those rows in ID order. This prevents a lower cursor committing after a
snapshot that already returned a higher one. Contention is measured in the
profile below; never replace it with an unsafe max(auto-increment ID) watermark.
The publisher performs network I/O outside the transaction, retries duplicates
with the same event ID and marks delivery separately. `on_commit` may wake it,
but cannot replace the durable outbox row.

```json
{"schema_version":1,"event_id":"ev_123","epoch":"e_1","cursor":"opaque","project_id":"pr_1","entity":{"kind":"puppet","id":"p_123","incarnation":"i_456","revision":"13"},"type":"puppet.changed"}
```

The internal namespace is versioned and installation-scoped. The publisher may
publish `mop.rt.v1.<installation>.project.<project>.events`; gateway service
identities subscribe only to configured installation/project subjects. A node
identity may receive only its own
`mop.rt.v1.<installation>.node.<node>.wake` hints and approved live progress
subjects, never arbitrary publish/subscribe or management verbs. Node durable
facts/results still go through authenticated DRF. Human and grant principals get
no direct NATS credentials: their realtime audience is resolved at the Django
gateway, and a session cookie/DRF token is not a NATS credential. Private addressed
streams use server-derived recipient identities and never the broad project
subject. #628 tests concrete broker ACLs, including denied cross-node/project
subscriptions and credential revocation. No extra Redis broker is required merely
to use Channels.

Events contain safe invalidation/projection metadata only, not secrets, messages,
transcripts or terminal bytes. Authorized clients fetch private details separately.
Projection/tombstone revisions are monotonic within an epoch; publication order
and client response order are not assumed. Retain durable resync history for at
least 24 h. Compaction/authorization changes can invalidate a cursor; correctness
comes from an explicit reset and new snapshot, not indefinite replay promises.

1. Client opens authenticated `/ws/v1/events`; the gateway validates Origin and
   current scope and subscribes to the internal event source before acknowledging
   readiness. It buffers bounded notifications while the client loads REST.
2. `GET /api/v1/snapshot?project_id=...` materializes rows and the matching project
   cursor under the short sequence-row transaction. For multiple projects return
   an opaque vector cursor, not a fictional global ordered counter. Paginated
   snapshots have a bounded server-owned snapshot identity/expiry; a page cannot
   silently mix revisions from different snapshots.
3. Client asks for events after that cursor. Gateway fills gaps from durable
   scoped REST/event history, merges buffered notifications and deduplicates event
   IDs/revisions. NATS delivery is only a wakeup; periodic cursor checks at most
   5 s apart detect a lost final notification.
4. Unknown/expired/foreign cursor, epoch/user/scope change, an overflow or an
   unfillable gap returns `reset_required`. Discard the old snapshot and pending
   projections, reauthorize and take a fresh snapshot plus pending-operation list.
   No assumption that successful subscribe proves a gap-free history.

One client store applies REST/WS snapshots by entity identity/incarnation/revision
and request generation. Reject stale REST after newer WS; tombstones prevent
resurrection; changing user/project/target resets generation. An event arriving
before its command response is ordinary; merge both by operation ID. A deleted
or new-incarnation target cannot be restored by an older response. Observations
carry source ID/incarnation, source sequence, observed/received timestamps and
fresh/stale/unavailable reason; nominal controller availability is not proof of
fresh node facts.

Terminal protocol is separate from events. REST authorizes an attachment/control
request before `/ws/v1/terminal/<session-id>` binds the Django browser session,
target/session incarnation and TerminalControl generation. Non-browser CLI attach
uses its credential-bound principal through the same scope policy; credentials
never appear in URLs. Browser sockets use the Django session, not a token copied
into browser storage. Only that control owner can send `input`/`resize`; input
frames carry a monotonically increasing connection sequence and are never replayed
after reconnect. Server frames are `output`, `ack`, `control_lost`, `closed` or a
safe `error`. An `ack` names the accepted sequence and whether dispatch to the PTY
was confirmed; it does not prove that the CLI executed the input successfully.
Lost acknowledgement means unknown delivery, not permission to replay keys.
Output is a bounded live stream, not event-history data. For example
`{"type":"resize","seq":1,"cols":100,"rows":30}` is valid only on an acquired
live writer connection.
Reconnect requires fresh authorization/control/PTY inspection; it does not create
a second PTY behind the first. Disconnect/revoke cancels subscriptions and releases
live handles with generation checks; a persisted control record is not evidence
that its socket or PTY is alive. Private history has a separately authorized read
endpoint and explicit retention, never a broad project event.

## Offline and repair boundary

Controller loss does not kill working puppets. New mutations, assignment claims,
credential issuance and control acquisition require current controller admission;
unavailable authority fails closed. Observations become stale. An agent may lose
unacknowledged offline telemetry without a durable local buffer; do not promise
every event survives while simultaneously removing that buffer. Usage reconciliation
and deduplication use provider request/attribution identity (#641/#656), not a
second invented billing log.

Cold boot is stdlib installer -> declared server dependencies/driver -> protected
DB/root bootstrap -> PostgreSQL -> migrations -> initial admin/config -> workers,
ASGI and realtime -> node enrollment. DB/initial Auth cannot depend on NATS or
working HTTP. Narrow privileged local/SSH repair calls the same services with a
trusted audited identity and direct configured DB connection. It is not a second
remote management API, blanket permission bypass or old file store. A remote DB
is valid; an unavailable DB causes a reasoned refusal or operator-authorized
backend restore. Backup/restore includes encrypted durable bytes, root-key custody
and a fresh epoch after restoration; #632 verifies a real PostgreSQL restore.
Mutation admission rejects a prior epoch, including an old retry whose operation
or tombstone was lost in the restored backup. Restore starts with effect dispatch
disabled until surviving external executors/resources have been reconciled and
fenced or proven quiescent. Restored claim counters alone cannot fence an effect
that continued outside the restored database. Only then can an audited recovery
open new admission/dispatch in the new epoch.

## Early slice, load budgets and acceptance

#632 supplies the PostgreSQL test databases, Django runner, ASGI/worker harness,
clean package install and fake-I/O boundary. #625 exercises actual REST
submit/status, permissions, request identity, claim, effect receipt and outbox.
#639/#640 add a real restart of one controlled new session, including fresh clone
facts, assignment and fencing; #628 adds a concurrent private stream. Final deploy
#644 is not a prerequisite for this early fixture. Do not bypass auth/fencing or
create throwaway production queues merely to show a successful request. Independent
schemas/policies can proceed in parallel; broad client migration waits for this
slice's recovery acceptance.

Reproducible baseline profile, established before measurement: one controller
instance (4 vCPU, 8 GiB), separate PostgreSQL (4 vCPU, 8 GiB), 3 test nodes,
30 controlled sessions across 3 projects, 10 concurrent HTTP clients, 10 event
sockets and 3 terminal sockets. After 2 min warm-up run 15 min steady state with
5 command admissions/s, 20 queries/s, 30 fact reports/s, 10 usage records/s and
1 KiB/s terminal output per terminal. Fake external effects normally take 100 ms;
one in ten takes 5 s. Use independent PostgreSQL connections/processes and
record versions, limits, hardware, dataset, seed and workload generator. A test
can scale hardware only by reporting a different profile, not silently replacing
the baseline. These numbers are provisional acceptance budgets, not measured
throughput promises.

| Metric | Initial budget under that profile |
|---|---|
| REST query / command admission | p95 <= 500 ms / 750 ms; admission never waits for external completion |
| Event-loop lag | p99 <= 100 ms, no single stall > 500 ms |
| Terminal input acknowledgement/output transport | p95 <= 200 ms excluding provider execution/network outside fixture |
| Database connection count | <= configured process/instance totals; no growth after soak/shutdown |
| Database lock waits | p95 <= 100 ms; bounded by configured 2 s timeout |
| Outbox publish lag/backlog | p95 <= 1 s; <= 500 unpublished rows at steady state |
| After 30 s broker outage | convergence/backlog drain <= 60 s after recovery; no lost committed result |

Actual admission capacity includes per-project/global queue caps: excess load
must return 429, not silently exceed memory/DB limits. Report backpressure counts
and distinguish admitted from attempted throughput. Slow effects must not starve
queries, terminal/revocation checks or result recording. Revise budgets only in
an explicit reviewed contract change with measurements, not to relabel a failed
run green. Re-estimate the epic after the slice, not after an Admin screenshot.

Acceptance owners and scenarios:

* #632: empty PostgreSQL migrations twice, system checks, missing migrations,
  packaging client without server extras, layer/import constraints, independent
  connection CAS/unique/lock/rollback checks on both supported PostgreSQL majors,
  broken DB connection, write/space failure, real backup/restore, bounded resources
  and async heartbeat during slow I/O. Other backends cannot substitute.
* #633/#635/#637: CSRF login/mutations, token revoke, inactive user, forged actor,
  list/detail/create/FK/M2M/Admin scope, LDAP source precedence/revocation,
  revoked grant ancestor/owner, wrong role/incarnation and addressed secret
  delivery before/after long waits; no plaintext in API/audit/Admin LogEntry.
* #625: simultaneous identical and conflicting request IDs, lost accepted/result
  response, worker restart, crashes before/after external start/effect/COMMIT/
  outbox publish, uncertain COMMIT lookup, late running effect after wait timeout,
  expiry and cancellation, permission loss before next stage. Confirm one effect
  or explicit unknown with claims held; never fake exactly-once external work.
* #639/#640/#648: old/new incarnation races, stale claims/results, fresh dirty or
  unknown clone facts, restart recovery, multi-target/resource ordering, each
  build/rotation compensation stage and conflicting newer recovery. No old pool.
* #626/#627/#628/#649: snapshot/subscribe gap, final NATS notification lost,
  WS-before-response, REST-after-WS, duplicate events, tombstones, changed epoch/
  user/scope/target, overflow/reconnect, expired history and endpoint receipts;
  competing terminal tabs, Origin rejection, logout during await, shutdown cleanup.
* #644/#645: new empty install through admin/project/node/puppet/send/observe/stop,
  service restart and restore; no removed mop state files, readers, obsolete
  imports/settings/dependencies/templates/endpoints or accidental legacy discovery.

Every pure/testable implementation uses red-before-green TDD. Documentation itself
needs no artificial failing test. The full strict suite and web:dist gate exact
issue and epic merge SHAs in CI; Django gates are added by #632, so today's old
suite cannot be reported as Django acceptance. Report source inspection, local
checks, CI and live experiments separately, including anything unverified.
