# New controller contract

This is the normative contract for the new installation in epic #623, established
by #624. It is a specification, not a claim that the current controller implements
Django or these endpoints. The implementation starts with #632. Existing subsystem
documents describe the deployed version unless explicitly marked otherwise.

The new controller is a modular Django monolith. Django ORM owns persistent mop
state, PostgreSQL is the production database, Django Auth owns the single Actor
identity and authentication for humans, agents and services, DRF serves commands
and queries, and Channels/ASGI serves realtime. NATS is an
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
| Python | 3.12; clients and nodes do not acquire server dependencies |
| Django | 5.2 LTS, maintained security patch selected and declared by #632 |
| DRF | 3.16 compatible maintained patch; synchronous dispatch/services |
| Channels | 4.x compatible maintained patch, selected and checked in #632 |
| ASGI server | Maintained compatible production server, declared in #632; not `runserver` |
| Database | PostgreSQL 17; the sole required compatibility combination is Python 3.12 + PostgreSQL 17 |
| Optional backend | SQLite may run fast pure/schema checks; not a supported controller runtime or concurrency gate |

Compatibility acceptance runs only Python 3.12 + PostgreSQL 17. One CI test job
runs the complete strict suite and every mandatory frontend gate — the dashboard
build check, full TypeScript checking and the complete Vitest interaction suite —
in the same Python 3.12 container with a read-only Node toolchain. Each gate runs
under its own exit status without fail-fast: any failure rejects the job, and a
failed Python check or an early web gate failure does not skip the remaining checks.
Other Python/PostgreSQL combinations are not required or guaranteed by this
contract. Existing bootstrap recognition of another version does not expand that
guarantee. These are support decisions, not claims that every backend Django
supports has been tested. Adding a backend requires the same migration, transaction, locking,
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
| Access | one Actor identity wiring (`AUTH_USER_MODEL`), membership, AgentProfile and agent definition versions, Delegation, creation intent; Django Auth adapters | authorize action, resolve actor, approve definition, issue/revoke delegation | Catalog; secret references through secrets public interface |
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

### Catalogue retirement seam (#634)

`Catalog.delete_project` and `forget_node` retire metadata only. They do not
remove a VM, job, image, clone, secret or retained FK/history. The catalogue keeps
`retired_at` and the advanced revision on the original UUID. Active uniqueness
uses ordinary composite constraints on name/origin plus `identity_slot`: zero for
an active target, its own UUID for a retired target. A check constraint ties those
two states together. This requires no conditional index or backend-specific SQL.
A repeated name/origin receives a new UUID; old requests still address the old row.

The trusted composition root may bind the narrow `catalog.retirement.RemovalGuard`.
Unbound removal refuses. `prepare_delete_project` / `prepare_forget_node` check
current authority/revision and call `inspect_project` / `inspect_node` **outside**
any SQL transaction. These calls return typed references to real retained receipts,
not `quiescent`, `empty` or actor callbacks supplied by a client. A project receipt
must cover all remaining jobs, including stopped jobs. A node receipt must cover
both scheduler and executor occupants, with complete source identity/incarnation,
sequence, actual receipt and bounded observation validity (at most five minutes;
an adapter may impose a shorter window). Missing observations are unknown, not empty.

`commit_delete_project` / `commit_forget_node` accept the prepared value and lock
the exact parent UUID. They reauthorize, check the expected revision/currentness,
reject configured node deployment membership and invoke the bound SQL-only
`validate_project` / `validate_node`. The validator rereads the actual receipt and
current authoritative source generations/coordination records; merely echoing the
prepared fields is not validation. It refuses absent, superseded, incomplete or
mismatched evidence. The catalogue validates the returned typed evidence and its
freshness before writing the tombstone. No inspection/network/protection call
occurs in this commit seam. Operations can put that seam and their own
result/audit/outbox writes inside one outer `atomic`; an error rolls all of them
back. No result/outbox implementation or generic callback is hidden in Catalog.

Later #639/#643/#648 composition must serialize job/body/build admission and
receipt invalidation on the same parent locks, deny retired targets and prove the
source/executor fencing that makes inspection safe. A fresh timestamp alone
cannot fence an independent external writer. If this cannot be established, the
production guard must refuse; this core does not implement external adapters,
physical effects, their recovery, or Operations publication.

`Catalog.project_is_current(id)` and `node_is_current(id)` are minimal internal
SQL queries over catalogue-owned models, not permission checks or public HTTP
endpoints. Access/SessionGate and the current-catalog composition root must use
those queries instead of backedge imports or treating any retained FK as active.
In particular a service Actor whose enrollment binds it to a retired/missing
node must be denied
at authentication/authorization, after waits and before addressed telemetry,
bootstrap, secret delivery or session stages. Retiring a node is not proof that
its old credential/process disappeared. This Access integration remains a
consumer task; #634 does not edit Access. Normal catalogue target/settings queries
and new writes exclude/refuse retired parents. Explicit historical policy reads
retain their history semantics and still require the owning access policy.

## Invariant ownership

The owner column names the single rule owner, not an exclusive file. Structural
constraints accompany each rule; validation on an in-memory model alone does not
protect a concurrent update.

| Invariant | Rule owner | Only mutation path | Transaction boundary | Required check |
|---|---|---|---|---|
| Project identity derives from origin via `driver.project_of` | Catalog policy | configure project | unique identity + expected revision | collision, renamed origin, forged project FK |
| Actor identity/auth: one `AUTH_USER_MODEL` Actor, kind classifies without granting, swappable FKs to the same model, PK survives incarnation change | Access identity policy (#683) | Actor migration/creation in the first fresh-install migration of its app | first-migration dependency ordering + swappable-user checks | duplicate User/Principal identity, kind-derived rights, PK churn on restart |
| AgentDefinitionVersion bytes/digest are immutable; program/runtime constraints are technical preconditions, not permissions | Access agent-profile policy (#686) | publish/approve definition version | create unique digest/revision; immutable after approval | changed bytes, partial approval, capability-dictionary rebirth |
| Delegation narrows only: issuer and subject are existing Actors, child never wider than parent, revoke preserves Actor and shared token | Access delegation policy (#688) | issue/delegate/revoke delegation | current issuer rights, bundle/incarnation, parent chain and CAS checked together | revoked ancestor/issuer, changed target, stale incarnation, disallowed spawn, widened child |
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

### Unified Actor identity (target, #624; implemented by #683–#689)

The single identity model is one Actor configured as `AUTH_USER_MODEL`, a
standard Django `AbstractUser` subclass. Humans, agents and technical services
are kinds of that one Actor, not separate User/Principal branches. Operational
rights for every kind come only from standard Django Group/Permission
(including direct `user_permissions`) through one application authorizer;
a `kind` discriminator classifies the subject and never grants a permission,
global scope or staff/superuser status. All identity references — actor, owner,
initiator, executor, delegator/issuer, subject — are foreign keys to this one
model with distinct technical roles in each action, never a second user table
or per-subject profile identity. Actor uses the standard user manager,
`createsuperuser` and Django's `UserAdmin`; there is no custom administrative
auth framework and no alternative User/ProfileID. An agent Actor is stable
across runtime incarnations: a restart or a new incarnation is an execution
record, not a new subject. AgentProfile/AgentDefinitionVersion and the current
RootProfile-style data are additional domain records linked to an Actor, not
other identities. Delegation (#688) is a narrowing relation between existing
issuer and subject Actors with lifecycle/expiry/revision/revoke; it never
creates a User or token per delegation, and revoking it deletes neither the
Actor nor the shared DRF Token. Enrollment, OS peer identity, assignment/lease
ownership and physical quiescence are explicitly named domain facts verified by
the same pipeline — not a second RBAC, GodManager or permission map.

This is the target contract. Since #683 the implemented identity is the single
`actor.Actor` (`AUTH_USER_MODEL`, a standard `AbstractUser` subclass whose
`kind` column only classifies); the former `Principal` table is deleted (#645);
since #688 a delegation (`access.Grant`)
binds an existing issuer and an existing agent subject Actor instead of a
Grant-linked service User, and #689 cuts the remaining consumers over; see AUTH.md.

Use standard Django User mechanics through Actor: password hashers and
database-backed sessions, `is_staff` grants Admin login eligibility only,
`is_superuser` is explicit global access but still cannot bypass target
identity, fencing or effect safety.

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
clients using it. Separate service Actors isolate machines/devices where required;
they are non-staff/non-superuser with unusable passwords and minimal permissions.
A service Actor cannot select a human owner in JSON.

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

ActorContext contains the credential-bound Actor, its classification, human
owner where applicable, scope and delegation identity. It is constructed at a
trusted boundary, not from the payload or a module-global current user. It is
a reference for live authorization, not a permanent capability snapshot, and it
never carries token/session keys.

| Change | New requests, pending/retry steps and secret issuance | Existing WS/terminal/delivery | Already running process |
|---|---|---|---|
| Browser logout | session invalid immediately; token independent | close connections/control bound to that session | do not kill puppet |
| Token revoke/rotate | old token rejected; browser independent | close token-bound endpoints; browser session unaffected | do not kill puppet |
| User deactivation | all actor paths rejected | close all bound streams/control/endpoints | do not kill puppet |
| Permission or membership removal | re-resolve current scope before start/retry/issuance | recheck before each protected delivery and after waits; close/filter lost scope | no automatic destructive rollback |
| Grant/ancestor revoke or owner rights loss | descendants cease to authorize start/retry/delivery | close delegation-bound subscriptions/control | reconcile any in-flight effect; no new stage; the Actor and its shared token survive |

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

The machine-readable subset of this contract is published as OpenAPI 3 in the
committed [API.schema.yaml](API.schema.yaml) (issue #696, generated by
drf-spectacular from the real views; regenerate with `mop dev openapi`, drift is
checked in CI). It covers the human/CI routes only; machine and private
protocols stay specified here and in tests, and the serving endpoint
`/api/v1/schema` is authenticated staff-only.

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

## Finite operation audit query (#626)

`GET /api/v1/audit` is a read-only projection of the existing immutable
`Operations.AuditEvent` evidence. It does not admit operations, repair state,
create a second journal or run effects. The mounted legacy `/api/journal` is not
an alias: `coverage` is exactly `["operation_audit"]`, not all historical bus
messages, terminal traffic or private command contents. The frozen contract is
also typed in [API.schema.yaml](API.schema.yaml); frontend migration uses this
contract rather than guessing a legacy-compatible payload.

Only these optional query fields are accepted, once each:

| Field | Contract |
| --- | --- |
| `project_id` | Canonical UUID of an explicit project intersection; never grants visibility |
| `limit` | Canonical decimal integer 1..100, default 100 |
| `cursor` | Opaque authenticated keyset cursor, 1..4096 characters; use the same filter and limit |

Every read requires the canonical `operations.view_audit` Django permission and
current credential, identity, RBAC and project scope. Actor kind or `is_staff`
is not installation-wide authority. Explicit all-project membership or Django
superuser status can provide current project scope, not a bypass of credential
checks. With no project filter, at most 100 currently authorized projects may
be materialized; a larger scope returns 503 `unavailable` asking for an explicit
project, never a truncated successful history. No current authorized scope is
403 `permission_denied`; an absent or unauthorized explicit project is 404
`not_found`. A valid scope with no matching evidence returns an empty page.

Visibility is decided in SQL **before** keyset pagination and `LIMIT`: every
pinned project of an operation must be currently authorized. A multi-project
event is disclosed whole, with all sorted `project_ids`, or not at all; a
project filter does not remove unauthorized project IDs from a visible row.
Puppet events on a shared node use the puppet operation's projects, not its
neighbors. Node-wide commands additionally require all current node projects.
Service enrollment restricts reads to its bound node even when another node
belongs to the same project. Delegated credentials additionally require the
current grant/owner/subject chain and exact target/incarnation. A build
operation's stored `node_id` is only an anchor, not proof of its complete build
node scope: node-bound service readers do not receive build audit events.
Project-authorized non-node readers see build targets as the actual project.

A successful response has exactly these fields:

```json
{"schema_version":1,"epoch":"<uuid>","results":[{"id":"<positive-decimal>","operation_id":"<uuid>","revision":"<positive-decimal>","created_at":"2026-10-18T10:05:00Z","action":"restart","state":"refused","code":"dispatch_declined","reason":"target does not receive dispatch","target":{"kind":"puppet","id":"<uuid>","incarnation":"<uuid>"},"node_id":"<uuid>","project_ids":["<uuid>"]}],"next_cursor":null,"coverage":["operation_audit"]}
```

`id` identifies immutable audit evidence, not the operation. `revision` is the
evidence's operation revision, encoded as a decimal string; `created_at` is UTC
RFC 3339. `target.kind` is `node`, `puppet` or `project`; `incarnation` is a UUID
for a bound puppet or null. Action/state/code/reason come from safe operation
evidence, including refusal reasons. No actor binding, request parameters,
private message plaintext/ciphertext, root/secret references, connection details,
environment, arbitrary payload or raw provider error is included. DTOs are
materialized immutable application values, not live ORM objects.

Ordering is descending `(created_at, id)` with an exclusive keyset boundary.
`next_cursor` is null only when the query has no further authorized matching row.
This is finite browsing, not a retained snapshot or gap-free realtime replay:
newer concurrent inserts appear on refreshing the first page, not in later
older pages; newly committed older evidence can appear below the boundary.
Cursors expire after 300 seconds and bind the installation epoch, live
credential/authorization proof, complete allowed scope, selected project and
limit. Tampering, expiry or a changed binding requires 410 `reset_required`
with `reset: {"required": true}`; the client restarts without a cursor. A current
authentication/permission revocation can instead refuse with 401/403. Scope and
installation/restore authority are checked again after materialization; no stale
successful page is returned when that check observes a change. Unavailable
restore/database authority returns 503. All successes and refusals are
`Cache-Control: no-store`. Mutating methods return 405.

## Durable operations implementation checkpoint (#625)

The first ORM/DRF vertical is **node drain/enable**, not a production replacement
for every operation in CONTROL. It uses the canonical catalog and Access migration
graph. `Operation`, `Step`, `EffectReceipt`, ordered durable resource claims,
`AuditEvent` and `OutboxEvent` are database records. Admission, claim, catalog intent,
result/application evidence and publication acknowledgment have independent short
commit boundaries. No operation model runs an effect from `save` or a signal.

Implemented endpoints are `POST /api/v1/nodes/<uuid>/commands/drain` and `/enable`,
`GET /api/v1/operations/<uuid>` and
`GET /api/v1/operations?request_id=<uuid>&epoch=<uuid>`. Node command envelopes use
schema version, epoch, client request ID, expected catalog revision, UTC deadline
and empty `params`; they accept no shell, subject or adapter selection. Catalog
node IDs are stable configuration identities, not puppet runtime incarnations.
Admission needs the specific `catalog.drain_node`/`enable_node` permission and
installation-wide membership; queries need `operations.view_operation` and current
scope. Executor confirmation uses a separately enrolled node-bound service actor
with `record_node_application`/`observe_node` permissions. A lost owner's rights
cannot authorize a new stage, but does not erase an already confirmed effect fact.

The worker accepts only the finite managed `set_eligibility(Claim)` adapter port.
After claim commit it rechecks current authority, target revision and expiry in a
separate durable dispatch checkpoint. The claim carries its immutable deadline;
the real receiver must also enforce it after transport waits, before the physical
effect. A boolean confirmation means the effect is finished and either applied or
known not applied; an exception/uncertain response records `unknown` and retains
every claim. Lease expiry, caller timeout and worker restart do not mint a new executor.
The test composition owns a managed fake adapter and real PostgreSQL connections;
there is no fake adapter or automatic-success workflow in the production root.
Safe authorization refusal phrases remain in the scoped operation result and
audit; the general outbox contains codes and revisions, not credentials or raw
exceptions. A confirmed receipt for an older configuration still records
quiescence and releases its claims. It does not mark a newer catalog revision
applied or overwrite that revision's observation; the result explicitly reports
`effect_confirmed_prior_revision` and `projection_updated: false`.

| Consumer or handler | Actual remaining dependency before cutover |
|---|---|
| Production drain/enable effects | #639 NodeExecutor adapter, runtime/incarnation and physical receipt fencing; bind its managed lifecycle in the controller root |
| Restart | The fixed identity/restart/readiness handler below uses the same Operation/Step journal. Production still denies admission without an approved physical capability/layout and exact policy binding; fresh/scheduler replacement is not this host-session effect |
| Send / dispatch | Typed admission, encrypted bounded input and machine receipts use the common journal as described below; production still requires explicit cipher, approval, endpoint and physical-support bindings. Socket write alone is not a delivery receipt |
| Create/update/recycle/delete/wipe, grant activation, credential rotation, terminal and maintenance operations | Finite subject-owned handlers and receipt/recovery rules; not an arbitrary callback registry and not accepted as implemented commands |
| Project build | #648 has finite DRF admission/status and the durable build process manager below; real scheduler/body/image consumers and privileged deployment remain root bindings, not automatic-success defaults |
| Worker process | Bounded Runtime-owned `Worker.run_one`/`run_pending` exist; root must wire the real executor, wake/poll and shutdown ownership rather than reuse the old web worker |
| Realtime publication | Durable outbox publishing seam exists, including duplicate event IDs after lost ACK; #626 connects authorized event fan-out and resync |
| Grants/Delegation | Access resolver injection is supported; unbound grant authority fails closed. Root must bind #635's reviewed resolver, never bypass it. Since #688 the subject is an existing agent Actor and rights are the RBAC intersection along the chain; #689 cuts the remaining consumers over without weakening these fences |
| Admin and CLI | #638/#642 call the same local service/DRF boundary; remove old admission/workflow owners only when these consumers are connected in the epic |
| Retention | Completed results become undisclosable after the 90-day prune boundary; request identity cannot be reused. Physical audit/receipt erasure still requires the operator's explicit privacy/retention policy |

This checkpoint does not authorize an old-pool deployment or imply that those
remaining handlers have passed transport, crash or performance acceptance.

### Bounded send and dispatch vertical (#625/#635/#639/#649)

`POST /api/v1/messages` and `/api/v1/dispatches` accept distinct finite commands:
`schema_version`, `epoch`, client `request_id`, `target_id`, `incarnation`, decimal
`expected_revision`, UTC `not_after`, and `params`. Parameters are `message`,
optional `priority` (`next|now|later`), `wait_seconds` (0–60), and
`notify_when_idle` (boolean). Admission limits the UTF-8 message to 16 KiB (the
machine application's bound) and the deadline to five minutes. Waiting never
extends the effect deadline or changes its outcome. No owner, force, role flag,
subject, path, shell, or adapter selector is accepted.

The current human or delegated source must have the exact send/dispatch action
and actual project scope. The target's active, sealed, approved receiving grant
is checked separately. Dispatch additionally requires the source's dispatch
capability and target `receives_dispatch`; it cannot take another human's
assignment. Ordinary send does not acquire/reassign human ownership. An optional
human holder comes only from authenticated `X-Mop-Endpoint` / `X-Mop-Connection`
binding; its live conversation is checked again in admission, claim and input.
`notify_when_idle` requires that holder. Its endpoint is separate from Assignment's
human owner. The private input carries the exact holder reply address and
notification request; delivery acceptance is not an idle or task-completion
receipt. The receiving application/report bridge remains responsible for emitting
the separately correlated idle report through #649.

Each accepted command creates one existing Operation, a typed MessageBinding,
the common puppet assignment pin, and exactly `apply_identity` then `send_message`
Steps. These are the existing machine WorkPort's work, not a second message
queue. The common node/project/puppet resource claims remain held between stages.
An active assignment and successful identity application precede delivery;
current initiating actor, target grant, incarnation, revision and deadline gates
run at claim and addressed input, including again after decrypt/provider waits.
The machine's own credential and lease are also rechecked after that wait.
Only an explicit `delivered` application evidence receipt confirms the final
stage. An uncertain write, lost receipt, lease expiry or executor restart retains
claims as unknown and cannot automatically redeliver the message.

Message/request/operation IDs are distinct. Admission and status return the
stable `message_id` as metadata. The canonical command is keyed-HMACed with an
explicit protected intent key. The recoverable input is retained only as AEAD
bytes bound to installation, operation, target and message identity; encryption
and decryption run outside SQL transactions. Admission repeats the complete
current gate after encryption. Unknown, confirmed terminal result and executor
retirement purge the ciphertext in the same journal transaction. Expired queued
input becomes inaccessible at the deadline and is purged/refused by bounded
`MessageHandler.expire_payloads`; machine polling invokes that cleanup. The
production worker root must also schedule it while nodes are offline. Minimal
IDs/digest tombstones and safe receipts follow the common operation retention
policy; audit/outbox/status never contain message text, ciphertext, connection
nonce, or keys. An identical existing request remains readable without a working
cipher provider or a still-live original target; it does not authorize execution.

`machinebinding.operations/compose` take explicit `message_cipher`,
`message_support`, approved `grants`, and optional `endpoints` ports. The default
root has no cipher key or physical send capability and refuses before admission;
it has no success adapter. The protected `/api/v1/machine/work/input` send schema
adds message, message/request correlation IDs, priority, notification request,
holder endpoint and private reply address to the fixed Git identity fields.
NodeHTTP validates that finite shape. The isolated PostgreSQL/TLS checks use
production DRF and NodeHTTP with explicitly controlled machine receipts, not a
claim of native application's delivery/idle acceptance or deployment readiness.

### Puppet create admission (#643)

`POST /api/v1/projects/<uuid>/commands/create-puppet` takes `schema_version`, `epoch`,
`request_id`, `not_after` and `params` with exactly `node_id` and `definition_id`; the
target, its incarnation, owner and name are the server's to assign, never the body's.
It needs a human with `execution.create_puppet`, `execution.assign_work` and
`operations.view_operation` in the project, a node of that project, an approved role
definition, the owner's latest approved unrevoked bootstrap revision, an available node
image whose observed revision is the desired one, and an executor epoch the node has
declared through `executor/restarted` (an empty node declares its first epoch there;
`NodeExecutor` keeps it). Missing pins refuse with no rows written.

Admission first checks everything without writing, then registers the target's agent
Actor and reserves and provisions its grant through the credential service under the
same human's credential -- the worker never holds it -- and finally, in one transaction,
creates the assignment named `pu-<project>-<n>`, activates the incarnation on the
declared epoch, records its launch pins (`IncarnationLaunch`: bootstrap revision and
digest, and a pin of the image revision) and the Operation `create` with the steps
`scheduler_register`, `apply_identity`, `confirm_ready`, `activate_grant`. Target and
incarnation ids derive from the actor and request key, so a repeated request finds the
same agent, grant and operation. The job is registered under the same addressed
metadata stop and delete read (`addressed_metadata`), by one conditional write
(`EnforceIndex` on 0) with a readback of the exact Meta. Until the installation binds
the credential service and a registering scheduler (#644) the production root refuses
admission as unavailable.

The worker runs `scheduler_register` as a scheduler lane like stop: claim, a last SQL
check, one conditional register, the record. The spec is rendered from trusted facts
only: the node's machine configuration, its scheduler enrollment (`ScheduledNode`: the
Nomad node id and declared launch capabilities, written by the trusted root, never by a
payload), the pool settings (`pool_home`, `pool_user`, `pool_datacenter`), the
incarnation's launch pins, the approved definition's role/digest/driver (`BundlePin`:
the controller never decrypts the definition) and the image policy's resources. The
step checks that the target's grant is still reserved and provisioned -- execution
authority would be a false refusal before the agent's token is delivered. Success
records `ScheduledPuppet`, so stop and delete recognize the job, and hands the next
steps to the node; the node never receives a server step as machine work. A lost reply
is `unknown`; reconciliation reads only, and like restart it does not resume the
remaining steps automatically. An existing job is never overwritten.

The node then runs `apply_identity` and `confirm_ready` of the same Operation on the
common machine protocol. They run under the target's provisioned but not yet active
grant (`authorize_launch`: reserved with its retained credential, the same owner,
project, target, incarnation, role and digest, and the owner's current
`execution.create_puppet`): activation waits for exactly these receipts and the agent's
token comes after readiness, so execution authority here would be a circle. Any other
action -- send, restart, wipe -- still needs the active grant. The create machine
handler is bound only with a launch support (the target's job is registered for this
incarnation); without it the machine route keeps refusing as unavailable. The last node
step is not terminal: `activate_grant` follows on the server.

#### Addressed launch delivery

`POST /api/v1/machine/work/launch` takes exactly the version-1 `Work` object,
not a definition UUID, project/owner selector or a reusable read capability. The
caller must present its own current token, a `node-executor` service enrollment
for that node, project/node access and canonical `execution.read_launch` permission
in addition to the permissions for its claimed work. A human, another service
purpose, another node/project or another executor's claim cannot read launch bytes.
Only the current claimed `apply_identity` or `confirm_ready` of `create` is eligible:
assignment revision/generation, incarnation, executor epoch, installation/restore
generation, operation/step/claim identity, deadlines and retained create resource
fences must still match. The server selects the definition from `CreateBinding`;
wire metadata never grants definition-read authority.

The rootless HTTP adapter forwards the original machine credential and Work through
the finite protected-socket `read_launch` command. Only `CredentialApplication`
opens the encrypted retained definition. Its Access-owned scoped reader uses
`authorize_launch`, the current reserved provisioned grant/ancestry, exact definition
and approved policy scope, seal/approval and retained references; it does not
construct an owner credential or broaden generic `read_definition`. The launch
pins must still name that owner's approved unrevoked project bootstrap and the
pinned project image revision. Provider I/O runs outside database transactions;
all authority, scope and pins are re-read after it and before disclosure.
Revocation, completion, expiry or rebinding during that wait refuses the response.

The no-store reply has exactly `schema_version`, `work`, `bootstrap_revision`,
`bootstrap_digest`, `image_digest`, `manifest` (the approved definition's JSON
object), `prompt` (canonical base64 of its actual approved bytes), `git_name`,
`git_email`, `message` and `peer_token`. Create uses the owner's current Git
identity, empty `message` and null `peer_token`: this endpoint does not issue an
agent credential. `LaunchResponse.parse` verifies exact Work/pins and the complete
bundle digest on the node. `MachineClient.read_launch` implements `LaunchQuery`
through the existing verified HTTPS transport's explicit POST allowlist; the
Work never appears in a URL. `AddressedLaunchSource` consumes this adapter.

Startup still has to bind the protected credential client in the web root and
supply the authenticated machine client to the node source (#644). Without the
credential binding the endpoint returns unavailable; neither web nor worker gets
a provider/root key. The scheduler remains metadata-only. This delivery does not
implement or bind the physical `SessionApi`.

`activate_grant` is discovered only once the registration and both node steps
succeeded. The worker asks the credential service to activate the grant -- activation
re-reads and re-checks the encrypted definition, so it can only run where the provider
is. The service's trusted activation receipt (`CurrentBindings.activation`) is the
target's current binding with confirmed identity and readiness of exactly the current
incarnation (`Assignments.readiness`, shared with addressed credentials); a mere
assignment projection is no receipt. The agent that becomes the delegation subject gets
its own local identity, project membership and token at admission, outside the grant
lifecycle (#688), or activation would always refuse. A refusal carries its phrase into
the operation; a lost reply is `unknown` and reconciliation reads the grant: `active`
succeeds the create, `revoked`/`expired` fail it, `reserved` proves nothing.

### Puppet delete vertical (#625)

`POST /api/v1/puppets/<uuid>/commands/delete` takes the version-1 target intent with
empty `params`; it needs `execution.delete_puppet` and belongs to the human who owns
the assignment. One scheduler step `scheduler_delete` first stops the job with the
same conditional write and physical readback as stop-job, then purges only the job
it stopped itself (a fresh consistent read must still carry this operation's stop
token) and proves the purge by a consistent 404. Nomad has no conditional
deregister, so a re-registration between that read and the DELETE would be purged
too; the window is short and documented, not hidden. A lost purge reply is
`unknown`; reconciliation only reads. On success the assignment is closed for good:
`deleted_at` is set, owner and current incarnation are cleared, `current()` no
longer returns it, activation refuses, and a database constraint keeps a deleted
assignment ownerless. The container body goes with the purged allocation; a host
body keeps its clone.

### Clone wipe vertical (#625)

`POST /api/v1/puppets/<uuid>/commands/wipe` takes the version-1 target intent with
empty `params`: no `force`, path or branch. Wiping destroys unsaved work, so it needs
its own `execution.wipe_puppet` permission and, like restart, belongs only to the
human who owns the assignment; taking over someone else's work is the separate
`override_assignment` capability, never a payload flag. It runs on the same machine
protocol and resource claims as restart, as one Step `wipe_clone`. The node executor
asks the trusted body port to prove the session is quiet; anything but an explicit
proof ends the operation `failed` with `failed-before-effect` and no destruction. It
then rechecks the admission and lets the body port wipe: what is removed is the body
driver's decision (the clone's accumulated state on a host body, the container on a
container body). Only a physically confirmed wipe is `clone-wiped`; anything else is
`unknown` and holds the target like any uncertain effect. Until the node composition
binds a body port (#643) the production root refuses admission as unavailable.

### Controlled host-session restart vertical (#625/#639/#640)

`POST /api/v1/puppets/<uuid>/commands/restart` accepts the version-1 typed restart
command above. `fresh: true` is refused before admission: scheduler/body/credential
replacement is a separate capability. The finite command registry selects the
node-eligibility or restart handler; callers cannot supply an import, method,
shell, argv, physical path, owner, node or JSON workflow. Operation target identity
is separate from its routing node. Node targets still have no incarnation.

A restart has three code-defined ordered Steps: `apply_identity`, `restart_session`,
`confirm_ready`. These are projections of the existing journal, not another queue.
The common resource claims cover puppet, node and project and remain held between
steps. Each stage result commits its common receipt, machine receipt, audit/outbox
revision together. The operation is succeeded only after the readiness receipt. A puppet result's
`evidence` names the last confirmed stage, its receipt ID, execution incarnation
and `confirmed` boolean. It does not invent a catalog `applied_revision` or claim
that restarting an application incremented the Assignment revision.
An uncertain stage or executor retirement leaves the aggregate unknown with its
resource fence; expiry or process restart does not release it or replay a stage.
An executor retired between confirmed stages also fences the unfinished workflow.

The controller-root `machinebinding` adapter consumes public Operations commands/
queries and `Assignments.current`/`target`; Sessions imports no Operations ORM.
Admission and current work checks use the initiating actor's current restart
permission and human ownership. The receiving grant is separately validated by
`Grants.authorize_execution(RoleBinding)` and its live ancestry, not by asking the
recipient role to have an outgoing restart capability. Execution actions use
their `execution.*` Django permissions directly (the logical `sessions.*` alias
is gone since #685), registered on real domain model content types by standard
migrations/post_migrate.

`GET /api/v1/machine/work/input?schema_version=1&operation_id=...&step_id=...`
is a private addressed input gate for the admitted service attempt. It repeats
current actor, assignment, grant, step and lease checks, and returns only the
current owner's bounded Git name/email plus the operation/step address. Missing
owner identity is refused before operation admission, and rechecked for each stage. It never
returns arbitrary command data, a message or credentials. Physical namespace,
clone, tmux pane, installed launcher and application socket come from the node's
trusted BindingResolver, not from these HTTP inputs. Production has no fabricated
positive capability or fallback path mapping: an unbound support port refuses
before an Operation is created.

`GET /api/v1/machine/executor?schema_version=1&node_id=...` supplies the previous
process epoch from server-owned current/replaced incarnations. An absent or
ambiguous registration refuses; the agent cannot derive it from a puppet name or
local marker. Restart acknowledgment uses the existing explicit previous/new epoch
CAS. Claim, result and restart acknowledgments cannot escape an outer uncommitted
caller transaction, including explicit autocommit-off transactions. External
fact, clone and inspection reads also refuse such a caller transaction. Restore
guard precedes Assignment and Operation/Step locks, including the expiry worker.

Run the disposable native fixture with the worktree launcher:
`mop dev controller --postgres 17 --label _restart_live_cases`. It uses Python 3.12 and
new isolated PostgreSQL/Docker resources, verified native HTTPS, actual disposable
tmux, Git and application Unix sockets; it checks old process-group termination,
new app readiness, owner identity and committed receipts/outbox. A second case
stops/restarts the HTTPS controller after the real effect but before its receipt,
then starts a new node Runtime: server epoch discovery/CAS retains unknown and
rejects the old late result without killing the live application or replaying it.
The fixture's approved policy and registered layout are explicit external test
ports, not production enrollment or proof of a real Claude/PVE rollout. Completion
within the admitted logical execution binding does not claim scheduler replacement
or a newly provisioned receiving grant/incarnation. Production binding of those
capabilities, protected payload sources and effect-specific historical inspection
remains cutover work; unknown is not an automatic retry path.

## Durable image builds (#648)

`POST /api/v1/projects/{id}/commands/build` accepts schema version, epoch,
request ID, expected project revision, execution deadline, an immutable policy ID,
`mode` (`missing`, `update`, `rebuild`), explicit `target_nodes` and boolean
`force_safety`. Build deadlines are bounded to two hours (not the short node
eligibility command's five-minute horizon); each owned subprocess is additionally
bounded by its remaining execution deadline. Branch, exact commit and manifest digest come from the saved
policy, not request-supplied shell/argv/paths. Acceptance requires current
`catalog.build_project`, `operations.view_operation` and their existing scope;
forcing clone safety additionally requires the actual `builds.force_build`
permission. Confidential policies are refused until protected manifest
materialization is bound; ciphertext is never treated as an executable manifest.

The handler uses the same `Operation`, `Step`, admission counts, request identity,
audit/outbox and resource claims. `BuildRun`, `BuildNodePlan`,
`AffectedTargetPlan`, stage intents and receipts are metadata with protected
foreign keys, not another queue or a generic saga description. The frozen narrow
#625 schema still requires a node field: the first requested node is only its
storage anchor; the real build target is the project plus every saved node plan.
The generalized #625/root integration must replace this structural anchor before
cutover rather than infer build scope from that one field. Claims for the
shared `build:mirror`, projects, nodes and affected targets remain held across
unknown outcomes and expired leases. The common worker discovers stale build
steps for inspection using the original token, never a new destructive attempt.

The complete affected-target plan commits before the first clear. Each target
records assignment identity, incarnation, revision, generation, role/grant and
exact source-spec bytes/digest. Clear, wipe, bake, restore and announce have
separate durable intents. Root returns the build thread's database connection
after each short transaction, before blocking external I/O. A previously
committed intent allows inspection, not blind replay. Restoration attempts every affected target whose clear was
attempted; newer identities/revisions are not overwritten. Original failures and
individual compensation failures remain separate. Process quiescence and a
known effect outcome are distinct persisted facts. An unknown-but-stopped process
still needs effect readback before resource release.

`missing` may skip only with fresh external availability for the pinned source
digest, persisted as addressed evidence. Desired ORM image state, project
registration or an Ansible exit code alone are not proof. Host nodes never have
container templates cleared/baked; mixed scopes send only PVE targets to the image
adapter. `GET /api/v1/operations/{id}` and request lookup expose bounded stage
progress, unresolved intents and failure details without a realtime subscription.

The privileged Unix executor checks both OS peer identity and the root-provided
Step/claim gate. `privileged_build_executor` binds independent SQL callbacks and
narrow safe refusal classes; already-safe library reasons survive unchanged,
while raw provider/OS exceptions expose only their type. It runs under a distinct non-root UID, never a node-bound
service Actor identity. Its finite protocol carries durable IDs and tokens,
not keys, inventory, shell or arbitrary arguments. Dispatch consumption commits
before privileged execution; replay is denied. Disconnect does not undo a result
or authorize resource release. The isolated Docker SETUP fixture proves separate
UIDs, an owned socket, rejected foreign peers, scoped dispatch/inspection and a
real owned subprocess. It is not a PVE build acceptance test.

Root composition supplies named Scheduler, ImageBuilder and NodeExecutor ports
and their capability preflight. `SafeAnsibleSubprocess` owns process groups through
timeout/cancel quiescence and bounds output/private log retention; actual known
secrets are redacted across pipe chunks and excluded from argv/environment.
Local process-group termination or a zero exit code does not establish remote
Ansible/PVE task quiescence: both normal completion and restart inspection require
external quiescence plus matching physical image evidence. Unresolved local
failure evidence remains visible alongside later receipts.
Inventory, playbook, private workspace, secret inventory and physical inspection
are trusted deployment bindings. The Unix client advertises image-builder
capability only when a fresh physical availability reader is also supplied.

Remaining integration boundaries are explicit: bind real conditional scheduler
and body consumers (including external spec/body identity fencing), physical image
revision/readback and Nomad announcement, wire confirmed catalog image projection,
protected manifest materialization, and configure the worker/executor lifecycle
and restricted OS/DB credentials in the installation root. No legacy build RPC
or file journal is used by this handler. Legacy consumer removal belongs to the
epic cutover; their presence in this source tree is not a runtime fallback.
Real builds on a new container/PVE installation still require operator-provided
resources. No old pool or image is an acceptance fixture.

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

Grant preparation reserves request identity and a pinned definition without granting
runtime authority: `reserved -> active` requires the matching creation/binding
receipt, or `reserved -> revoked/expired` terminates an unactivated reservation.
An active delegation transitions only to `revoked/expired`; issuing a delegated
child checks the active ancestor chain, issuer rights and pinned approved policy.
Logical revocation denies new acts immediately; transport-credential retirement
is a separately recorded fallible stage, never a reason to keep logical access,
and it never deletes the subject Actor or the shared DRF Token. Revocation never
reactivates an old delegation. Role edits create a new immutable definition
version and require a new approval; they do not change existing digest bytes or
the agent Actor: `repin_definition` moves the pinned program of a stable agent,
with CAS on the profile revision. Activation is
not implied by a SQL assignment, a lease or claims in a payload: the unbound
activation and transport-retirement executor denies, and an assignment
projection alone is not an activation or retirement receipt.

The Access ORM boundary exposes `Grants.publish_definition`, `approve_definition`,
`read_definition`, `register_agent`, `repin_definition`, `agent_profile`,
`reserve`, `reservation`, `provision`, `activate`, `revoke`,
`expire`, `record_retirement`, `resolve_actor` and the explicit message/dispatch
admission methods. `reserve` accepts either a credential-bound human Actor or an
actively delegated agent Actor, never an owner/holder/subject/force or workspace
payload. The delegation subject is resolved on the server: the agent Actor the
same human owner registered for that target with `register_agent`; an
unregistered or foreign-owner target is a 404 before any write, and no
delegation step creates, deletes or re-keys an Actor or a DRF Token. A
delegated issuer additionally needs `access.issue_grant` and
`execution.create_puppet` itself, intersected with its chain and owner. CreationIntent
retains the first approved policy SecretVersion reference; retry queries that
intent instead of fetching the parent's latest policy or creating a second child.
The injected approval query must validate the exact stored intent binding, not a
boolean approval supplied by a caller. A grant's public definition read returns only
its own pinned role, not other private prompts owned by the same human. Preparation owns the approval evidence;
Access owns only its immutable reference and the creation request identity.
`register_agent` is the only agent-kind Actor creation path: it creates the
stable agent Actor and its AgentProfile domain record in one transaction, or
returns the existing profile for the same target idempotently; no grant, restart
or assignment lifecycle creates or deletes an Actor.

`RetainedSecrets` is the narrow Secrets composition port used for definition bytes
and a delegation's generated transport credential (never the subject's DRF
Token): `prepare(actor, RetentionScope, bytes)` performs
AEAD outside SQL; `commit(actor, PreparedRetention)` repeats target-bound authority
and inserts those exact prepared IDs/envelope in the aggregate's transaction.
`read(actor, scope, version_id)` is an internal protected read with checks before
and after provider I/O, not a general API serializer.
`check_reference(scope, version_id)` and
`validate_policy_reference(version_id, project_id=...)` return no ORM object or
plaintext; they validate current same-target or approved-policy references and
are safe local checks inside the aggregate transaction. The scope names the actual
project, human owner, aggregate UUID and purpose. This authority is explicitly
bound by Access from the stored intent; it does not grant human secret-management
permissions merely to provision a role's own credential. The standard DRF Token
row is the framework's credential-bearing plaintext exception described in
STORAGE.md; AgentDefinitionVersion, CreationIntent, Delegation, events and public metadata
contain no raw token. Delegation metadata refers to the encrypted SecretVersion
instead.

Approval, runtime-binding/activation/retirement queries and message/dispatch
executors default to denial. Binding queries are trusted local authoritative
reads, not network calls inside transactions. Later Preparation and Sessions
composition supplies these queries and the physical delivery executor; an
unbound executor refuses before an allowed effect. `token_context`,
`current_actor` and `Access` take an explicit delegation resolver; browser
sessions and node service Actors cannot become delegated contexts. Delegation
plain-send admission carries no assignment claim or git identity; dispatch
admission carries the current human owner for the separate fenced
identity-before-send operation.
No legacy registry reader, credential import or dual write is used by this path.

Rotation transitions are `staged -> delivering -> active -> retiring -> retired`,
with `reconciling` on uncertain delivery/revoke and `failed` only on a known safe
failure. Confirmed partial application plus proven executor quiescence moves to
`recovery_required`, preserving the known receipt subset and safe reason. It may
start explicit `restoring_old -> old_restored -> revoking_new -> compensated`
steps, never replay the original effect. A partially applied compensation ends
in `partial` with both original and compensation reasons retained; it does not
pretend rollback completed. Current authorization, version/incarnation and
external ownership are rechecked before each recovery effect.
Activation requires the configured addressed receipts; old credentials
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
  Allowed kinds are `states`, `clone`, `sizes`, `disk`, `junk`, `health`;
  token `usage` is not a node fact (#656), but a separate proxy consumer;
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
| WorkPort v1 | `pending(node_id, executor_epoch, project_ids, limit)` filters current authorized project IDs before applying the bound, and returns immutable WorkV1 projections; `current(operation_id, step_id)` rechecks initiating actor/operation authority; `claimed(work, attempt_id)` records existing step admission; `completed_result(work, attempt_id, outcome, evidence)` records its result; `retired(target_id, executor_epoch)` fences unfinished workflows even between confirmed steps | No independent queue, no external I/O. Current/claimed/result participate in caller's short atomic transaction. Idempotent by attempt ID; an unknown commit is reconciled, not replayed. |
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

## Private client endpoints (#649)

The session app's Django label is `execution`, not Django's authentication
`sessions` app. `endpoint_models.py` adds ClientSession, Endpoint,
ConnectionLease, MessageDelivery and DeliveryReceipt; `models.py` only reexports
them. A client conversation is unique by authenticated human User, owned catalog
ClientRegistration and exact external CLI conversation ID. Display names do not
participate. Endpoints are project-specific, have opaque `private:<uuid>`
addresses and a 60-second renewable presence TTL. Presence is always a database
registration projection (`live:false`), never a socket health assertion.

Standard Django session/DRF token authentication runs first. Human registration,
presence and delivery reads require current project view permission/membership,
current ClientOwnership and the corresponding `execution.register_endpoint`,
`execution.view_delivery` or `execution.deliver_report` permission. Human report
send checks the sending actor's project and deliver permission; it does not grant
access to the recipient's client registration. An exact private address is a
routing identity, not a bearer capability. A delegated subject may send when it,
every subject of its chain and the human owner hold `execution.send_message`,
checked by Access's local current-grant resolver (#688);
they cannot register a human endpoint, enumerate human sessions or acquire their
transport context. Services cannot use human reports. No human endpoint operation
changes a Nomad job or reads host socket registries.

`GET /api/v1/addresses?schema_version=1&project_id=<uuid>&limit=100` returns only
owned endpoints, with a maximum 500 rows. `GET /api/v1/addresses/<endpoint-id>`
has the same private scope. `POST /api/v1/endpoints` accepts exactly
`schema_version`, `client_id`, `project_id`, `external_session_id`, `display_name`.
The owner and credential are never payload authority. Registration creates no
subscription and returns no connection nonce.

A trusted gateway subscribes an explicitly bound peer first, then calls
`Endpoints.connect` to acquire a 15-second ConnectionLease. PostgreSQL locks and
shared constraints choose one receiver across gateways; gateway ID, connection
nonce, installation epoch and increasing generation fence replacements. Expired
leases do not prove old effects stopped. An old `attempting` message becomes
`unknown`, loses retained payload bytes, and is never replayed to a replacement.
Unattempted queued messages can be claimed by the same endpoint's new connection
before their original deadline. Restore readiness/epoch gates run in the root;
its installation lock also serializes shared admission caps (64 pending per
endpoint, 256 overall). No network call holds that transaction.

`TrustedEndpointContext` lives in `mop.common.endpoint`, without Django or Access
imports. It is returned only by trusted connection binding and rechecked on every
use. It includes the actual human User, client-conversation identity, endpoint,
credential, gateway, nonce and generation. #625 must carry an optional holder
endpoint separately from Assignment's human owner: a User/token alone cannot
identify which human CLI conversation owns work. A nonce is a transport binding,
not another authentication backend or a replacement for current Django auth.

`POST /api/v1/reports` accepts `schema_version:1`, current installation `epoch`
(obtained from the authenticated health query), `address`, UUID `message_id`,
`text` (at most 64 KiB), aware `not_after` (at most 24 hours), and optional UUID
`operation_request_id`, boolean `notify_when_idle`, priority `next|now`. The
message identity is distinct from its delivery ID and operation request ID.
A stale installation epoch is rejected before cipher/admission, including when
restore lost the old message row. Disabled installation effects deny new reports
and are checked again before dispatch, not only when a connection is acquired.
An identical sender-Actor/message key in the current epoch returns stored
metadata even after presence expiry or cipher unavailability; a changed intent
returns 409.
Admission returns 202 and a delivery Location, never proof of receipt. Private
content is retained only as purpose/recipient/delivery-bound AEAD bytes, purged
on terminal delivery state. Results expose only IDs, state and idle evidence.

`GET /api/v1/deliveries/<id>` is private to the original sending Actor or
recipient human, under current project/client authority. A current connection
can `POST /api/v1/deliveries/<id>/ack` with exactly `schema_version`, `outcome`
(`accepted|unknown|unavailable`). `X-Mop-Endpoint` and `X-Mop-Connection` headers
bind that acknowledgment to the authenticated current receiver; neither header
chooses the actor. The same headers authorize addressed endpoint `/renew` POST
and endpoint DELETE. An acknowledgment before a durable attempt, changed receipt,
foreign recipient or old connection is rejected. `accepted` means explicit
application acceptance, not successful completion of a task. Idle observation is
a separate correlated immutable receipt, not acceptance or task correctness.
No message content is exposed through events, audit or these metadata reads.
Expired queued payloads are purged by bounded admission cleanup; they cannot
permanently occupy shared queue capacity. Expired attempts become unknown, not
replayable. Identity/receipt metadata is retained without automatic pruning in
this core; retired addresses and message keys are never recycled. A deployment
retention/erasure policy must preserve those non-replay tombstones.

`create_endpoint_runtime` composes a bounded Runtime and DeliveryGateway. It owns
peers, DB calls, effects and shutdown; peer cleanup and generation-checked lease
release run before the DB executor stops. The gateway can drive bounded ticks
without holding an HTTP request. Before dispatch and after each external wait it
rechecks receiver credential, client ownership, project scope and lease; pending
sends also recheck the initiating actor/grant. Disconnect/revoke does not convert
an uncertain write to a known failure or release it for replay.

`mop.client.local_bridge` is stdlib-only. Its trusted launcher supplies OwnSession
with the exact own CLI socket, process and external conversation ID; remote JSON
cannot supply them. The bridge checks canonical private UDS ownership and kernel
peer PID/UID, never scans neighboring sockets or reads primary/legacy state files.
It sends bounded native `msgV:1` user frames with the admitted message ID and a
private ephemeral callback socket. Only a matching `message_accepted` callback
confirms application acceptance; an explicit `message_refused` means unavailable.
Successful socket writes without that evidence are unknown. `notify_when_idle`
uses a distinct control message ID and matching `peer_idle_notice`; terminal
input/keystrokes are never queued or replayed. The launch adapter must replace
this binding on external conversation changes even when PID/path remain the
same; a socket alone cannot attest to a conversation ID. Same-UID OS processes
are not a sandbox.

These core use cases and an actual isolated Unix peer are implemented/tested by
#649. Production Channels/WS authentication, broker routing and connection loop
binding remain #628; native CLI launch/conversation lifecycle and MCP wiring remain
#642; protected cipher deployment remains #669/#644. The DRF root deliberately
leaves the cipher and grant resolver unbound until that composition supplies
them, refusing private content admission instead of giving web/gateway a root
key. The isolated fixture explicitly binds the real AEAD provider and current
Access/delegation authority. No old pool or native vendor CLI acceptance is
implied by that fixture.

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
facts/results still go through authenticated DRF. Human and delegated agent
Actors get no direct NATS credentials: their realtime audience is resolved at the
Django gateway, and a session cookie/DRF token is not a NATS credential. Private
addressed streams use server-derived recipient identities and never the broad
project subject. #628 tests concrete broker ACLs, including denied
cross-node/project subscriptions and credential revocation. No extra Redis
broker is required merely to use Channels.

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
uses its credential-bound Actor through the same scope policy; credentials
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
that its socket or PTY is alive. A bound writer in `unknown` retains the exclusive
SQL fence: lease expiry, REST detach, cancellation and detach timeout do not prove
physical retirement. An unbound reservation can close without a physical proof.
Before physical start, pin the instance ID and conversation/terminal epochs to the
credential/Actor, target/incarnation/executor, gateway/connection and generation.
Every write attempt and resize checks current authority after waits and a synchronous
physical identity guard immediately before the syscall. A retired instance cannot
reopen; closing a borrowed attachment never changes the body owner's FD flags or
kills its process. A consumed input sequence with unknown outcome cannot replay.

The trusted Root resolver must explicitly provide `preflight`, `resolve` and
`inspect`. Preflight refuses unsupported physical identity/fencing/retirement
capabilities before WS bind; resolving a reconnect must not spawn a body. The
current local adapter requires an already nonblocking PTY, a synchronous node-side
guard, and actual conversation/terminal epochs. `RootTerminalStream.inspect` and
`reconcile` are public composition-service seams, not extra WS commands or mounted
HTTP endpoints: inspection must identify the original durably pinned physical
instance, and reconciliation repeats current authorization after inspection.
Only a Root-issued, instance-bound retirement capability can close the old SQL
fence; neither a JSON quiescence flag, an ORM row, a substitute retired handle nor
`LeaseExpired` qualifies. A lost Root needs trusted inspection of that original
instance; if this cannot be established, the fence remains unresolved. Default
Root is deliberately unbound; remote physical node integration remains #643, not
a claimed supported terminal backend.

Private history has a separately authorized read endpoint and explicit retention,
never a broad project event.

## Proxy usage source (#656)

CLIProxyAPI, not session transcripts, is the source of new token accounting.
The source-tag profile `cliproxy-v8.0.16-accounting-2` describes public upstream
**v8.0.16 source**, not the version installed on any existing pool. The trusted
credential-runtime binding (#669) must validate the actual binary/schema and
controlled request counters before issuing `ReadCapability`. An unsupported or
unverified instance is denied, not guessed from its version string in settings.
No production queue/configuration was read or changed to implement this profile.
Profile evidence is the tagged [accounting codec](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.16/sdk/cliproxy/usage/accounting.go)
and [queue implementation](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.16/internal/redisqueue/queue.go),
not a promise that a deployment matches those sources.

The public query is `GET /api/v1/usage?schema_version=1&start=YYYY-MM-DD&end=YYYY-MM-DD`.
Dates are UTC, start inclusive/end exclusive, at most 366 days; `limit` is 1..100
and `after` is the opaque returned `next` cursor. Results are materialized daily
source/key/provider/model/alias aggregates. `execution.view_usage` requires both
its action permission and installation-wide scope; project-only credentials may
not read globally unattributed shared-key traffic. Each counter carries a decimal
string or null, `known_events`, and `unknown_events`. Known subtotals do not claim
full coverage. `total` is a separate parent metric: never sum it with its input,
output, cache or reasoning components. Unclassified structural zeros are not
measured category zeros. This is token accounting, not monetary billing.
`user`, `project`, and `puppet` attribution is unavailable: the shared client key
proves none of them. `mop.client.usage.stat` is the bounded DTO adapter for #642;
#626 consumes the same REST query, not a parallel node collector.

`UsageSource`, `UsageReadIntent`, `UsageEvent`, and `UsageAggregate` belong to the
execution application. Before each destructive GET, a short sync ORM function
commits a read intent; external I/O never runs under SQL locks. A single source
row and unresolved-read constraint serialize independent connections/processes.
Expiry is not quiescence: a delayed or crashed read holds that source until a
trusted, addressed `Quiescence` confirms its old HTTP attempts cannot still pop.
Recovery records a permanent `read_outcome_unknown` gap, never replays the read.
Event identity is source plus hashed execution ID; a separate payload fingerprint
detects collisions. Identical events do not increment aggregates; conflicting
payloads retain the original and record an explicit gap rather than overwriting.

The async collector owns one batch (at most 100 records/2 MiB), and sends whole
materialized ORM transactions through its owning Runtime. The finite `UsageBoundary`
port holds management credentials and key-label HMAC material in the server-only
credential process; raw queue records never cross into public DTOs/logs/events.
No domain module imports RootController or retrieves a credential itself. The
production boundary is deliberately unbound/denied until #669 supplies it.

A local DB lock is **not** cross-installation ownership. Upstream v8.0.16 supplies
no remote lease/ack/replay API. Its queue is volatile RAM (retention at most one
hour); restart/expiry can lose records, and stream subscribers divert publications
away from the backlog. Empty queue therefore never proves complete coverage.
`source_complete` is always false. Read-intent/gap status is queryable independently
of an empty period; unknown lost-event counts are never invented as zero.

Before enabling a fresh installation, #644/#669 and the operator must demonstrate
an actual upstream access boundary: only the named consumer can reach the queue
and usage stream, including management panels, direct management access, other
processes and other mop installations. Gateway #647 must deny these destructive
queue/stream routes to panel clients. Two separate database leases or a declared
"exclusive" boolean are insufficient. If that access topology cannot be enforced,
collection stays denied. Controlled live acceptance must verify the deployed
version/profile, client-key identity, model/alias and provider-specific counters,
prove exclusion and test restart/retention loss. Isolated fake HTTPS transport
checks are not that live proof. No existing production queue may be sampled by a
second test consumer. The transcript collector is removed only with the connected
#626/#642 cutover; no fallback or blending of historic transcript totals is allowed.

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
* #683: empty PostgreSQL 17 migrations to the first administrator, human/agent/
  service Actor creation, one `AUTH_USER_MODEL`, standard Group/Permission,
  session/DRF Token and swappable FKs, stable Actor PK across incarnation
  change, no kind-derived rights, repeat migrations, system checks, migration
  drift and native backup/restore of the new schema.
* #684: one authentication contract for human/agent/service Actors — revoked
  credential, forged ActorContext, deactivation, rotation before first use and
  during I/O, LDAP immutable-subject change, single-session logout, CSRF, no
  plaintext in context/logs.
* #685: parameterized human/agent/service authorization matrix — equal rights,
  scope and required facts give equal decisions; group/direct permission
  grant/revoke, inactive Actor, foreign project/client/node, staff without
  permission, duplicate logical permission, unknown action, spoofed scope;
  LDAP nested groups, ambiguous identity and source-owned revoke.
* #686: agent Actor stable across reassignment, restart, incarnation change and
  separate delegation revocation; history/audit stay bound to the correct
  Actor; immutable definition bytes/digest/approval; no foreign work.
* #687: service Actor technical enrollment matrix — forged bound node/kind/
  owner, other node, stale epoch, enrollment revoked during await, foreign
  incarnation, incomplete physical proof all refuse; real distinct-UID socket
  and independent PostgreSQL connections.
* #688: Actor with two independent delegations — revoking one disables neither
  the other nor the identity or shared token; expiry, permission downgrade,
  issuer deactivation, parent revoke; wider child refused; stale context after
  token rotation cannot adopt a new identity; foreign target/project/
  incarnation/forged holder refused.
* #689: one parameterized contract matrix across DRF/Admin/WS/worker/private
  boundary for human/agent/service: same effective rights, scope and factual
  preconditions give the same decision and audit; only the credential transport
  differs. Legacy resolver paths, sessions→execution aliases and duplicate
  policy maps removed, verified by imports/discovery and regression tests.
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

The #683–#689 deliveries do not wait for one another's full old-consumer
acceptance: #689 owns the adapter cutover and parity matrix, while each
functional task keeps its own public contract; #645 closes the architecture
matrix after #644 and the #689 cutover. Creating a task does not start its
implementation, a master merge or a deploy.

Every pure/testable implementation uses red-before-green TDD. Documentation itself
needs no artificial failing test. The combined strict-suite and frontend-gates job gates exact
issue and epic merge SHAs in CI; Django gates are added by #632, so today's old
suite cannot be reported as Django acceptance. Report source inspection, local
checks, CI and live experiments separately, including anything unverified.

## Project configuration HTTP (#634)

`POST /api/v1/projects` accepts exactly `origin` and optional nullable
`puppet_limit`. It calls the shared Catalog creation use case and returns 201,
`Location`, and the canonical project identity and decimal-string revision.
A duplicate name/origin is a 409 conflict, not an implicit update.
`PUT /api/v1/projects/<uuid>` accepts exactly `origin`, nullable `puppet_limit`
and a positive decimal-string `expected_revision`. It calls the same aggregate
configuration use case as Admin; an obsolete revision returns 409 without writes.
These are configuration changes, not physical build/delete operations.

Both boundaries require current Access authorization, the action-specific Django
permissions and installation-wide scope required by Catalog. Staff status alone
is insufficient. Foreign project scope remains 404. Unknown or duplicate JSON
fields, coerced numeric revisions and boolean limits are refused. The JSON parser
bounds input before validation; session mutations require CSRF.
Responses are no-store. Project deletion remains unavailable here until a trusted
external-removal proof is bound; it is not exposed as a direct ORM delete.

## Unified read implementation seam (#626)

The initial query owner is `sessions.projections.PoolQueries`, constructed with
an explicit live SQL-only `epoch` reader. A cached ControllerRun is not a query
authority port; the root rechecks the restore fence and current installation on
each call. DRF
`GET /api/v1/puppets`, `GET /api/v1/snapshot` and the read-only Assignment Admin
use it; `GET /api/v1/puppets/<uuid>` returns a `puppet` object. List/snapshot
responses contain `schema_version`, installation `epoch`, `results`, server-owned
`snapshot_id`, an opaque project-vector `cursor`, and nullable `next_cursor`.
`project_id` is a canonical catalogue UUID; `page_size` is 1..500, default 100.
Unknown/repeated query fields fail. Display names and aliases are never authority;
Assignments currently have no published runtime-name binding, so `name` is null
and `aliases` is empty rather than reconstructed from a reused name.

A puppet object contains `kind`, UUID `id`, current `incarnation`, decimal-string
projection `revision`, `deleted`, actual project/node IDs and display names,
`desired`, receipt-derived `applied`, freshness-bearing `observed`, pure-policy
`state`, and explicit proxy-usage availability. `desired.revision` is the current
Assignment CAS revision. It is **not** interchangeable with the read-projection
revision: the latter also advances with persisted source/publication changes and
catalogue revisions. Assignment commands consume the owning aggregate's CAS
revision, never this display/publication counter. The typed client `PuppetView`
preserves both `revision` and `desired_revision`; its `target` property binds the
UUID and current incarnation to `desired_revision`, refusing deleted targets or
an unavailable incarnation. CLI restart resolves that authorized detail unless
the caller supplies the complete explicit epoch/incarnation/CAS binding. Server
admission still rechecks the current binding; a client snapshot is not frozen
authority. A generic Snapshot preserves the wire fields but does not substitute
one counter for another. Browser projection convergence and complete publication
coverage remain separate integration requirements. Nullable application metadata uses
`applied_revision`, not an entity `revision: null`; #642 validates every entity
revision as a decimal string. False applied receipt predicates mean unconfirmed,
not evidence that an external application was reversed. No legacy row-format
adapter exists.

At PostgreSQL read committed, the authoritative row, catalogue joins, applied
receipt predicates, source observations, projection clock and project sequence
are correlated in **one ORM SELECT**. There is no `prefetch` or lazy ORM work in
the projector. Scope and installation epoch are reauthorized/rechecked around
materialization; a change returns reset, never a mixed success. Initial limits
are 5,000 targets, 2 MiB of materialized safe rows and a 4,096-byte opaque vector
cursor. A scope whose vector exceeds that wire bound must select a project;
it never receives a cursor that the client cannot submit. A one-page read performs no
writes. Multi-page reads retain only a derived ORM snapshot for five minutes,
with at most eight active snapshots per user; an explicit bounded maintenance
port removes expired snapshots. Subsequent pages serve those original rows even
if an independent transaction commits. Every page rechecks current credential,
project scope, epoch and controller run. Unknown/expired/foreign cursors reset.
This is a database consistency boundary, not a simultaneous snapshot of nodes.

`ProjectionWriter.begin_run(epoch)` is an explicit controller startup hook. GET
never initializes a run, repairs assignments, polls sources or starts effects.
Authenticated `GET /api/v1/machine/observation-context` returns the current run
challenge. `POST /api/v1/machine/observations` accepts exactly schema version 1,
`controller_run`, target/incarnation UUIDs, `producer_epoch`, decimal-string
`sequence`, `kind`, UTC `observed_at`, `payload`, `status` and finite safe `reason`.
The receiving credential must be a current enrolled service for the actual node
and project, with the independently registered `execution.ingest_observations`
permission. `execution.read_facts` alone cannot write observations. A producer is
explicitly enrolled by `POST /api/v1/machine/observation-producers`, naming the
controller run, target/incarnation, **separate** executor and producer epochs,
kind and expected registration revision (`"0"` only for known absence). Retry of
the same binding is idempotent; replacement uses CAS, and minimal immutable
ProducerEpoch records prevent reusing a retired producer epoch. The addressed
`GET /api/v1/machine/observation-producers/<target-uuid>/<kind>` returns the current
registration without creating one. The producer must match that registration and
the current execution incarnation. Old run/incarnation/producer replies are
refused, and an equal/older producer sequence cannot refresh facts merely by
incrementing a controller counter. Executor epoch is never implicit enrollment.

The current finite source kinds are `state` (explicit session status/alive/listen),
`clone` (dirty/ahead plus optional branch/default branch) and `size` (disk bytes).
Each retains producer identity/sequence, observed/received times, controller run,
and freshness/reason independently. State/clone expire after 30 s, size after
300 s. Unobserved/stale facts are unknown; an explicitly unavailable source is
silent, never free/dead. Only fresh session/clone facts reach `state.verdict`.
Freshness is evaluated at materialization time; frozen pagination rows keep that
snapshot's values. Consumers must honor each immutable `expires_at`, not cache a
`free` verdict indefinitely because an entity revision stayed unchanged. The
#627 presentation/expiry adapter and server freshness publication lifecycle still
need integration; this core does not invent a new read counter to hide that gap.
Both ingest and read validate the payload whitelist; history, messages, terminal
bytes, environment and secrets cannot pass through generic JSON. Inventory is
not this endpoint: empty, partial or unknown inventory never means deletion.

`ProjectionWriter.changed`/`retire` are SQL-only mutation-owner ports. The owning
composition takes `lock_projects` before aggregate locks and invokes publication
inside the **same** atomic block as its aggregate/result/audit changes. The port
advances the persisted per-target clock and locked per-project sequence and
appends a safe outbox invalidation; rollback removes all of them. It imports no
Operations private model or controller root and installs no global signals.
`events_after` and `GET /api/v1/events?cursor=...` read that scoped durable history.
Its cursor is a signed project vector, not max(auto-increment ID). Retired target
UUIDs remain tombstones and cannot be revived by late observations. A recreated
name must be bound by its owner to a new UUID.

`SourceCollector` accepts explicit async source and SQL sink/database ports,
bounded batches (500), concurrency (at most eight) and deadlines (at most 30 s).
Kinds have independent lanes; source I/O occurs outside database callbacks.
Raw transport failures are explicit safe categories, not successful empty facts.
A trusted adapter's typed `SourceRefusal` and SQL/use-case refusals preserve their
already-safe reasons and drain sibling tasks; they are not sanitized again or
hidden behind a generic task-group message. No production
scheduler/telemetry/proxy collector is silently selected by these ports.

Remaining integration is deliberately visible in `coverage` and
`handshake_complete: false`, not represented as an empty successful product:

* Catalog/Assignment queries and addressed observations are real. Existing
  mutation consumers must bind the explicit publication port before aggregate
  mutations can claim complete outbox coverage; #625's separate operation outbox
  is not imported or relabeled as this project-vector history. Pending Operations
  are not fabricated as an empty list. The current #642 Snapshot convenience field
  defaults omitted pending operations to an empty tuple: consumers must inspect
  coverage and cannot treat that default as a completed recovery handshake.
  Rechecked #642 at `20c2a4c5`: it now accepts omitted error epochs and the
  foundation's finite error codes. When the restore authority is unavailable,
  this reader omits the unknown epoch rather than sending null or inventing an
  identity; successful pool snapshots always carry their verified epoch. The
  foundation's separate `/api/v1/projects` responses use a dedicated catalogue
  codec, not this pool snapshot envelope. The current pool query
  scope is human credentials; grant disclosure needs its current Access scope
  adapter bound at the root, not a synthetic human membership or token fallback.
* #639 FactPage v1 `states` has running/dirty/session-count fields, not the complete
  session status/liveness facts required by this `state` schema; its clone facet
  also lacks the required ahead/unpushed evidence. It lacks the separately
  enrolled producer and this controller-run challenge. It cannot be translated
  into fresh/free facts by assigning a new read counter or inventing clean data.
* The developing #641 `Latest` query has no public projection revision or complete
  source/session binding in its result, and its read path currently creates an
  observation. That is not a compatible side-effect-free roster source. Freeze a
  non-mutating current-session/model query before composing it into this snapshot.
* #649's published EndpointPresence identifies the real external conversation and
  reports registered/live separately. It has no monotonic entity revision or
  retirement/incarnation version for #627 convergence. No User/ClientRegistration
  row is substituted for that missing versioned endpoint query. Its `addresses`
  query is owner-private and requires delivery permission; pool-view permission
  cannot silently broaden that audience. Human endpoints remain a distinct kind,
  never puppet capacity or a Nomad action target.
* #656 is the sole usage source. Rechecked its published `8843d7e8`
  `UsageQueries.read`: it exposes global key/model/date aggregates under
  `execution.view_usage`, explicitly declares user/project/puppet attribution
  unavailable, and has a separate non-snapshot page/wire contract. Those totals
  cannot be attached to an individual puppet or disclosed using pool-view
  permission. Per-puppet usage therefore stays explicitly unavailable; no
  transcript usage collector is imported. Scheduler/image observations without
  producer generations remain unverified.

These seams do not claim CLI/WS integration, a gap-free subscribe handshake,
external effect parity, deletion of every old reader elsewhere in the repository,
or acceptance on a new live installation. Those require the owning consumers and
combined epic checks; old-pool fallback is not an integration strategy.
