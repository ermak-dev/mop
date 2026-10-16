# Storage authority

## Release boundary

This is the storage contract for the new Django release, specified by #624
in epic #623. It is not a description of the currently deployed file-backed
release, and publishing this document does not switch an installation.
[CONFIG.md](CONFIG.md) distinguishes the two releases. [API.md](API.md) is
the canonical contract for the API, authentication, authorization, secret
visibility and privileged repair; this document defines persistence and
ownership, not a second security protocol.

The new release is a **fresh installation**. Django models, migrations and
the ORM are the only durable authority for mop-owned state. There is no
import of the old installation, old-file discovery, read fallback,
compatibility writer, dual-write period or automatic adoption of jobs found
in another installation. An unavailable or invalid database is an error,
not an empty registry and not permission to consult old files. Django schema
migrations evolve the new database; they do not migrate legacy file stores.
Old installations and their work remain untouched until the operator makes
an explicit, separate installation decision.

“Only durable authority” does not mean “no files anywhere”. Git worktrees,
external services, process sockets and generated consumer files still exist.
It means that no restart, read, authorization decision, retry or reconciliation
can recover mop intent from a second authoritative filesystem store.

## Three kinds of state

| Kind | Authority | Rule |
|---|---|---|
| Desired state | ORM records accepted by the domain service | Installation settings, managed node definitions, projects, puppet definitions, role bindings, approved bootstrap policies and secret versions express what mop intends. |
| Observed state | The system being observed | Nomad allocations, node health, clone dirtiness, live sessions and proxy availability are facts with a source and observation time. Persisted ORM observations are labelled snapshots, never desired state or proof of current health. |
| Generated state | A recorded ORM revision plus a renderer | Specs, inventory, playbooks, credentials delivered to consumers and service configuration can be rebuilt. A consumer acknowledgement records what revision was applied; reading its generated file must not overwrite desired state. |

A successful database commit proves intent was accepted, not that Nomad, a
node or a proxy applied it. Records of operations must distinguish acceptance,
dispatch, observed effect, refusal, failure and unknown outcome. A timeout or
a lost response does not prove failure and must not trigger blind replay.
An observation must not clear a lease, approve a policy or grant a capability.

## Runtime authority replacement matrix

The left-hand columns name the existing source-tree paths and their active
readers/writers so that replacement can be checked at runtime, not just by
creating new tables. Paths under `~/.config/mop/` belong to the relevant
process account, not necessarily the interactive operator. Model descriptions
below are logical responsibilities, not promises of particular Python class
names. The domain service owns mutations through ORM transactions; CLI,
HTTP, MCP, workers, deployment and local maintenance use that same domain
logic. Remote clients do not connect directly to the controller database.

### Controller configuration, identity and credentials

| Existing authority or input | Existing writers → readers | New authority and replacement readers/writers |
|---|---|---|
| Clone `.env`; `config.SETTINGS`, `REQUIRED`, `SERVER_SCOPED` and `NODE_SCOPED` in `mop/common/config.py` | Operator and `settingsedit` → dispatcher, server services, `playvars`, specs and deployment | Typed installation settings and revisions in ORM; configuration service validates writes and all runtime setting readers resolve there. Code retains schema and defaults, not installation values. Protected process bootstrap is the narrow exception in CONFIG. No general environment-over-DB precedence survives. |
| `inventory.yaml`, group/host overrides and `inventory-hosts.json` | Operator; deployment renders the JSON → deployment, node provisioning and `cluster` forget guard | Managed nodes, membership, addresses, drivers, resource policy and inventory membership in ORM; provisioning renders inventory from a committed revision, and forget checks ORM membership. Live Nomad capacity remains an observation. |
| `project-asks.json` | Deployment collects `.mop/sandbox.yaml` → `spec.read_asks` | ORM project resource requests/pinned manifest revision and installation ceilings; project registration/build records the accepted Git input, spec construction reads ORM. No deploy-time JSON authority. |
| `secrets/operators`, configured `MOP_OPERATORS_FILE`, `/etc/nats/identity/operators` | `identity` user administration and deployment copy → file identity provider, callout, owner identity, web login | ORM local users, password hashes, roles and project access; identity service writes and all authentication/authorization readers use the shared provider. The generated file is not a login provider or fallback. Passwords are hashed with Django password facilities, not reversibly stored. |
| LDAP configuration and `ldap-bind.pass`, including the copied identity directory | Operator/deployment → `ldapauth`, `identity.server_provider`, callout and web sessions | ORM provider settings and encrypted bind secret; identity adapter reads them. LDAP remains external authority for its identities, credentials and groups; ORM local policy/provider mapping is separate. A cached role is not indefinite authority when revalidation fails. |
| `secrets/nats-service.pass`, `nats-callout.pass`, `nats-node-*.pass` and `/etc/nats/base-users.json` | Deployment password generation and rendering → NATS, service/agent connection setup, `creds` | Encrypted ORM machine credentials with identity, purpose and version; credential service creates/rotates, NATS renderer and authenticated delivery read the selected versions. Deployment does not generate another password when a file is missing. |
| `puppets/nats-puppet-<project>.pass` | `natsconf.passwords` → NATS rendering and bootstrap project credential delivery | Encrypted ORM project credential where that protocol still needs one; project/credential lifecycle owns creation and removal. Role-only puppet authorization uses the grant records below, not a project password shortcut. |
| `secrets/llm-proxy-client.pass`, `llm-proxy-mgmt.pass` and service-account copies | Deployment; proxy rotation adapter → proxy setup, catalog readers, panel gateway, bootstrap and `proxykey` | Encrypted ORM mop-owned proxy credentials and versions; proxy adapter, gateway, catalog and delivery read them through authorized services. CLIProxyAPI's provider credentials are a different external store. |
| `servers/<address>/bootstrap.json`, `~/.config/nomad/bootstrap.json`, `NOMAD_TOKEN` | Nomad ACL bootstrap and deployment copy → `creds.token`, `nomad` and scheduler configuration | Encrypted ORM integration credential once issued; only the controller's Nomad adapter can retrieve it. Nomad owns the ACL/token lifecycle itself. No master, node or puppet receives a Nomad management token; the old environment/file selection is removed. |
| `secrets/tls.key`, `tls.pem`, configured TLS paths, CA pin copies | Deployment/operator certificate source → nginx, credential collection, TLS verification, proxy delivery | Mop-managed certificate/key/CA bytes and version metadata in ORM, private bytes encrypted; delivery materializes restricted files for consumers. An externally managed certificate/CA remains externally owned input with an explicit reference and renewal policy, not a second mop record store. The encryption root key is never a certificate stored in ORM. |
| `/etc/nats/callout-issuer.seed`, `callout-xkey.seed` | Callout key setup → `callout.keys` and `natsconf.apply_callout` | Encrypted ORM signing/encryption seeds; credential service owns provisioning/rotation, callout/config renderer consumes scoped versions. A public key or fingerprint may be exposed, never the seed. |
| `~/.ssh/mop-bootstrap`, Git deploy keys and their distributed copies | Deployment/key generation or operator provisioning → bootstrap SSH, image builds, Git cloning and node seeding | Mop-owned private key bytes are encrypted ORM secrets; bootstrap/build services materialize minimum-permission temporary or generated consumer copies. Externally managed SSH credentials remain external inputs, not an alternative mop metadata registry. Public keys/known-host trust are versioned configuration, not inferred trust from an arbitrary file. |

### Projects, puppets, policies and durable workflows

| Existing authority or store | Existing writers → readers | New authority and replacement readers/writers |
|---|---|---|
| `projects.json`; historical `projects`, `limits.json` | `cluster` project verbs and deployment seeding → registry/list, project admission, specs and deployment | ORM projects with origin, derived project identity and nullable nonnegative limit. Registration/deletion/limit domain operations own writes; every admission/list/build reader queries that authority. Basename identity remains `driver.project_of`; conflicting origins are refused. `0` means frozen, null means unlimited. Neither old snapshots nor the removed pair are imported. |
| Nomad job `Meta` and task environment used as puppet definitions | `cluster` add/update/recycle/stop/delete and `spec` → cluster, bootstrap, agents, web and CLI | ORM puppet desired definitions, incarnation, owner, origin/branch, pinned role/policy, lifecycle intent and external job reference. Lifecycle service commits desired changes; renderer submits derived jobs. Existing jobs/allocations are observed through Nomad, never used to reconstruct a missing puppet definition. |
| `souls/registry.json`, including `request_id`, `intent`, activation/revocation and job binding | `grants.prepare/activate/revoke/cut` → callout provider, cluster authorization, bootstrap resolve and target resolution | ORM grants/principals, human owner, project/job/incarnation binding, request reservation, activation and two-stage revocation. All grant readers query the authoritative records. Uniqueness and transactional transitions prevent duplicate reservations; a retry with changed intent is refused. |
| `souls/nats-grant-*.pass` | `grants.prepare/cut` → callout password check, bootstrap role credential response | Encrypted ORM grant credential, linked to the incarnation and revocation state; grant service creates/cuts it. Authorization revocation and transport credential removal stay distinct. No credential in a public binding, job spec or response log. |
| `souls/bundles/<digest>/manifest.json` and prompt bytes; packaged/external role catalogs | Catalog selection and `grants._store_bundle` → grant resolver, bootstrap and body preparation | Immutable ORM approved role manifest and exact prompt bytes with canonical digest and provenance. Catalog files are publication inputs, not live authorization state. Approval pins a snapshot; later source edits cannot change an existing grant. Runtime resolvers load the stored snapshot and validate its digest. |
| `bootstrap/<puppet>-tasks.yml`, `-vars.yml`, `-sent.json`, `-approved.json` | Human lifecycle request, `bootstrap.store/note_sent/note_approval/copy_approval` → bootstrap execution, inheritance, registration deduplication and give-up logic | ORM immutable bootstrap policy bytes, parsed execution snapshot, provenance, registration marker, digest and human approval, linked to puppet/incarnation. Human lifecycle domain operation accepts the snapshot; role children inherit that exact approved revision. Empty approved policy differs from missing approval. Files are execution materializations only. |
| `bootstrap/<puppet>-result.json`, `-progress.json`, `-progress.log` | Bootstrap runner → CLI result polling, give-up decisions, web bootstrap progress | ORM bootstrap runs, attempt counters, allocation/incarnation/registration association, progress chunks and sanitized log bytes, terminal outcome and timestamps. Runner writes, result/progress readers query ORM. Restart must not lose failure counts or confuse a previous run with the requested one. Logs needed by these readers are database content, not paths to retained files. |
| `project-secrets/<project>/files/<path>`, `vars.env` | Cluster secret put/remove → secret listing, bootstrap allowlist filtering, delivery to bodies | ORM secret names, types, encrypted file bytes/variable values and versions under project ownership. Secret service writes; bootstrap reads only the pinned role allowlist. Paths are validated logical destination names, not server storage paths. Project deletion has an explicit secret retention/deletion policy; reusing a name must not expose the prior project's secrets. |
| `landing.json` | Cluster landing verb → landing arbitration | ORM project landing lease/token, holder and timestamps; landing service acquires/releases transactionally. Restart/deploy does not release it, and local file presence does not grant it. |
| Clone `.git/mop-owner` and the node's claim/undo generations | Agent ownership preparation/rollback, send/terminal identity installation → lease gate, safety checks, roster and Git identity hooks | ORM ownership/lease and claim-generation records; ownership service controls compare-and-transition and compensation. Agent clone dirtiness/live-session facts remain observations. A generated owner marker or Git identity hook is not authority and cannot approve a takeover when the controller is unavailable. |
| `proxy-key.json` distribution fingerprint and `proxy-rotation.json` checkpoint | `proxykey` distributor and rotation start/resume → distributor/status/resume, `proxyapi` effects | ORM rotation operation, encrypted old/new secret references, step checkpoints, frozen recipient set and per-recipient applied version. Rotation/distribution service writes, status/recovery reads ORM. A fingerprint is not a recoverable key and cannot replace encrypted key bytes. |
| Web `sessions.json` | `websession.SessionStore` login/logout → HTTP authentication | Standard Django database-backed sessions and Auth records as specified by API; login/logout own creation/revocation, request authentication rechecks current access. No custom session-file loader or in-memory-only success when persistence fails. |
| Admin operation registry/deduplication and per-target lanes in memory; historical `ops.jsonl` and payload files | `adminapi.Api` accepts, `opworker` executes → result polling, SSE and operators | ORM operation identity, authorized actor, target, idempotency/request intent, durable execution/observation checkpoints and terminal result; operation service and workers own transitions. Recoverable sensitive payload is encrypted and retained only as required. The currently active code does not have a durable `ops.jsonl` queue: removed historical files are not an import source. |
| Service audit lines to journald and NATS events; web collector journal in memory | `service`, cluster/bootstrap journals, agent and worker events → web `/api/journal`, CLI/operator logs | ORM append-only domain audit records linked to actor, operation and target, with safe reason and time; the committing service/worker records them. Journald remains operational diagnostics, NATS/SSE delivery is notification, and a web in-memory ring is a view. None is a durable substitute for the database audit record. |
| Build lane/progress in `builder` memory, image availability/digests and job reservations | Builder/cluster and image adapters → builds, admission and progress consumers | ORM build requests, pinned inputs, desired artifact version, attempts, checkpoints and results. Build service writes; readiness is a separately observed artifact fact. Image files themselves stay artifacts and can be rebuilt; their existence does not create desired project/build state. |
| NATS events, local master inbox presence and transient idle/report delivery tracking | Channel/MCP/agent delivery and event publishers → discovered addresses, reports, idle notices and event consumers | ORM addressed endpoints with expiry/incarnation, delivery intent, encrypted payload references where recovery needs bytes, and deduplicated receipts. Registration/delivery services own updates; only live authenticated endpoints authorize delivery. Offline observations can be lost; no promise of a durable local spool or exactly-once messaging. |
| Web snapshots/event rings and ephemeral publication callbacks | Web collector/service publishers → snapshot, journal and realtime clients | ORM project sequence, projection/tombstone revisions, epoch, immutable outbox and bounded event history. Aggregate services commit state/audit/outbox together; publisher records publication separately. Snapshot/resync readers follow API.md's cursor protocol. NATS notifications cannot substitute for these rows. |
| Node-local usage aggregation over session `.jsonl` and `.meta.json` | Session runtime writes, `usage.py`/agent read → stat and web aggregation | Retained ORM usage/attribution observations and reconciliation records, including source identity, project, puppet/node, initiator and reporting window. Reconcile/deduplicate using the canonical provider request/attribution contract in API.md; do not invent a second billing ledger. Missing observations are unknown/excluded with reasons, not zero. Runtime-owned transcripts stay external. |

### Client, node and execution surfaces

| Existing local surface | Existing writers → readers | New authority or explicit non-authority |
|---|---|---|
| Human `servers/<address>/connection.json`; machine `operator.json`, `client.json`; local `tls.pem` | Login/deployment/credential collection → `creds.snapshot`, `bus.server_config`, session environment, models and diagnose | ORM server/client bindings, identities, trust and credential lifecycle; authorized API supplies the binding. Local initial server address/token/trust bootstrap is narrowly scoped in CONFIG, not another editable profile registry or an ORM bypass. Human legacy pair loading is already removed by #662; the new release also has no connection-file authority. |
| Git config `mop.server`, `mop.user`, `mop.branch` and persistent selection implied by ambient `MOP_*` | Login/operator → `context.clone_binding`, dispatcher, issue-branch base and master session setup | ORM client/workspace bindings for selected server, identity and integration branch. Explicit per-invocation selection is validated context, not durable configuration. An old clone's config/environment cannot silently select a different authority. Git's actual branch, remotes and commit graph remain Git-owned facts. |
| Node `bus.json`, `node.env`, `secrets.env`, `bootstrap.pub`; body bus/secret files | Deployment, distributor, driver and bootstrap → agent, wrapper, driver and session | Authenticated, least-privilege generated delivery from ORM configuration/credential revisions. Agent acknowledgement records applied revision in ORM. These are execution inputs, not fallback configuration databases. A node never receives the database login or root key. |
| Body `souls/<name>/<inc>/bundle`, `bus.json`, `ready.json`; generated hooks/MCP settings and bootstrap files | Driver/agent preparation and wrapper → wrapper, role/session checks and runtime | Generated, incarnation-bound artifacts from ORM-approved snapshots. Ready receipts attest local preparation, not authorization. Validate expected identity/digest; reauthorization and revocation read the controller authority. Missing/corrupt materialization fails, never selects a looser role. |
| `.local/state/mop/turns`, `models`, session registries, statusline data and usage from runtime transcripts | Session hooks and session runtime → `session.py`, agent state, roster, usage aggregation and model-switch observation | Live runtime remains source of observations. Mop-owned retained observations/history of outcomes live in ORM with session/incarnation, timestamps and expiry. Local hook spools can be bounded transport buffers only, not restart authority or a durable history API. Runtime-owned transcripts and messaging sockets are not mop configuration. |
| Terminal attachments, input sequence receipts, stream buffers, inboxes and web SSE/cache state | Node/server terminal registries, channel/session sockets and web collector → attached clients | TerminalControl ownership/generation is an ORM record shared across workers, distinct from human Assignment and ExecutionClaim. Connections, PTYs, input receipts and buffers remain ephemeral under API.md's terminal protocol; restart requires fresh authorization/control and never replays input. Other receipts/results promised across restart must be ORM records. An expired handle cannot silently attach to a new session. |
| Browser local/session storage, including panel preferences | Web UI → same browser | Optional per-viewer presentation convenience only. No identities, authorization, jobs, operations, shared settings or secret authority there. Durable shared preferences, if offered, are ORM records. |
| Clones, mirrors, compiled outputs, package copies, image/template files, generated inventory/playbooks and diagnostic exports | Git, builders, drivers and deployment → runtime/build tools and diagnostics | Source, artifacts, disposable caches or explicit exports, not mop state stores. Their paths may be metadata, but a path cannot stand in for authoritative policy, secret or log bytes. Diagnostic output must redact secrets. Old export markers and legacy registry snapshots are never startup inputs. |

This matrix includes readers that currently use files even where another
component originally generated them. Calling a file “generated” without
replacing those readers does not satisfy the contract.

## External authority boundaries

* **Git** owns repository history, source manifests and actual working trees.
  Mop stores the accepted origin/ref/commit, exact approved role/bootstrap
  bytes and desired branch separately. Fetching source for a new request is
  allowed; re-fetching mutable source to reconstruct an existing approval is
  not. Dirty/unpushed work cannot be inferred from an ORM desired revision.
* **Nomad** owns scheduling, allocations, health, live job state and its ACL
  system. Mop owns desired puppet/node policy and integration intent. Job
  specs are derived submissions; external identifiers and timestamped
  observations are ORM records, not a second intent registry. No secrets in
  job specs or metadata, even encrypted secret payloads.
* **CLIProxyAPI** owns provider accounts/OAuth credentials, provider quota,
  cooldown, model discovery and its own internal persistence. Mop does not
  import its auth directory, logs or database into Django or replace its
  internal storage. Mop-owned settings and API-key lifecycle are ORM intent;
  a management adapter applies the supported external API and records
  observed outcomes. Editing proxy files behind that adapter is not a mop
  configuration write. Provider model availability is observed, not a durable
  permission grant.
* **NATS, LDAP, SSH and the session runtime** retain their protocol/process
  responsibilities. NATS transport is not an auxiliary mop database or durable
  queue. LDAP identities remain externally owned. Runtime transcripts,
  sockets, process identities and live Git facts stay external observations.
  Their adapters do not introduce file-based mop authority.

## ORM and database portability

Production uses PostgreSQL, with supported versions and mandatory database
CI gates defined by API.md. Persistence is expressed through Django models,
ORM queries, transactions and Django schema migrations, with portable field
and constraint semantics. Optional SQLite schema/pure checks are not evidence
of controller concurrency correctness or a supported SQLite runtime. Adding
another runtime backend requires API.md's full backend acceptance gate;
portability must not be claimed from a passing single-process check.

Use foreign keys and explicit lifecycle rules for ownership; portable unique
constraints for principal/request identities and digest versions; portable
checks for valid limits, revisions and lifecycle values. Keep authorization
facts relational rather than burying them in an unvalidated JSON blob.
Opaque structured payloads may use validated ORM JSON fields; authoritative
bytes use database text/binary fields. Do not use `FileField`, an object-store
URL, a filesystem path, SQLite opened outside Django, pickle, a JSON directory
or a vendor-specific side database as the actual state behind an ORM row.

No raw/vendor SQL, PostgreSQL-only schema types, extensions, advisory locks,
triggers or database-specific indexes may be required for correctness. A
backend's optimization cannot become an unstated semantic dependency. Portable
constraints and transactional conditional transitions must protect competing
writers; a process-local lock alone is not a database concurrency contract.
Test races, retries, expiry, revocation and recovery, including the chosen
backend's actual isolation/locking behavior.

Commit intent and its audit/operation linkage together. Deliver external
side effects only from committed intent, and record dispatch and observation
separately. A database transaction cannot atomically commit a Nomad request,
SSH command or proxy rotation; recovery reconciles the stored request identity
with the external effect before retrying. Database failure must reach the
caller with a safe reason. It must not create a memory-only session, accept an
unrecorded operation or regenerate a credential.

## Encryption, authoritative bytes and retention

All recoverable secret values owned by mop are encrypted before persistence
using authenticated encryption (AEAD). This includes project file/variable
bytes, machine/grant credentials, integration tokens, private keys and
sensitive operation/policy content. Human passwords use Django password
hashers. Browser sessions and CLI/API tokens use the standard Django/DRF
mechanisms selected in API.md, not a bespoke encrypted authentication system;
their database records are credential-bearing and require protected storage,
backups and administration. No password/token value appears in diagnostics.

Each encrypted value carries a versioned envelope, key identifier, nonce and
authentication tag. Nonces must not repeat under a key. Associated data binds
the value to its installation, record identity, field/purpose and secret
version so that swapping ciphertext between records fails authentication.
The encryption root key is supplied from protected local bootstrap outside
the database, repository, generated specs, public API and logs. It is not
stored beside its ciphertext as a database row. Rotation must preserve key
identifiers and decryptability until re-encryption and verification complete;
unknown key identifiers or failed authentication are hard errors, not an
empty secret or fallback to a file.

Maintenance composition uses `controller.bootstrap.secret_provider` with the
verified Installation identity/fingerprint and an explicit retained key identifier.
It reads the protected root once, checks the fingerprint, then derives a 32-byte
AEAD key using HKDF-SHA256 (RFC 5869) from every root byte. The fixed extract salt
is `mop.secret.root.hkdf-sha256.v1\0`; expand info is `mop.secret.aead\0`, the
ASCII installation identity, a NUL separator and the ASCII key identifier. The
single expand block ends with byte `0x01`. A public fingerprint is never used as
key material, and changing the identifier changes the derived key. The root must
contain at least 32 random bytes, not a human password. This factory supplies one
retained root version; rotation composition must explicitly retain providers for
old identifiers until all referencing envelopes are safely retired. It does not
grant root access to ordinary web, worker, gateway or node processes.

When Secrets is installed, native restore authenticates every retained SecretVersion
in bounded batches against its actual relational context and envelope metadata,
using the separately protected bootstrap root. This runs after the new epoch and
`effects_enabled=False` commit, but before the independent restore guard becomes
ready. A corrupt tag, incompatible key, inconsistent metadata or unavailable
storage leaves the guard closed for maintenance recovery. Matching the public
root fingerprint alone is not proof that the encrypted archive is readable.
The verification assumes the restore precondition: services and other writers
are stopped; it does not create a concurrent online re-encryption procedure.

Bootstrap storage retains the exact accepted source bytes, validated execution
snapshot, approval/digest relationship and per-run log/progress content. A
hash plus a path is insufficient: losing the path must not lose the approved
policy or its evidence. Sensitive policy/vars/log content is encrypted; logs
are redacted before retention and are access-controlled even after redaction.
Do not retain raw subprocess output containing secrets merely because the
column is encrypted. Materialized files are private execution inputs with
bounded lifetimes; they are never read back as the canonical policy.

Retention is explicit for operations, audit, bootstrap runs, observations,
revoked credentials and secret versions. API.md owns the minimum operation and
event-history windows and request-id tombstones; result expiry must not make
an old request key executable again. Apply these lifecycle rules:

| Record/artifact | Removal boundary |
|---|---|
| Project/node | Refuse project removal with remaining jobs, including stopped jobs, and node removal with occupants or configured inventory membership. Explicitly reconcile external removal; a database cascade cannot prove it happened. |
| Puppet/incarnation and grant | Revoke future authority and retire addressed endpoints/credentials; retain identity, audit and unresolved effect evidence. A reused name gets a new identity, never the old grant. Deleting metadata does not implicitly destroy an unexamined clone. |
| Role/bootstrap snapshot | Immutable once approved; retain while a live grant, puppet, child approval or unresolved run references it. Do not delete bytes and retain only a digest/path. |
| Secret version/rotation | Remove plaintext execution materializations when no longer needed. Keep encrypted versions required for active delivery, safe retirement or unresolved compensation; authorize final retirement/deletion and remove stale generated copies deliberately. |
| Operation/delivery/audit/outbox | Retain pending/unknown-effect evidence until reconciliation. Expire payload bytes separately from safe receipts/results; preserve request tombstones and retained event history. Publishing an event does not erase its operation result. |
| Sessions/client bindings/terminal control | Logout/revoke/expiry invalidates the corresponding authority per API.md. Persisted control never proves a live PTY; removing a client binding does not kill a working puppet. |
| Observations/usage/logs | Apply declared time/size bounds, retain provenance and explicit gaps, and protect private reads. Do not prune evidence still needed for a safety decision or unresolved run. |
| Generated files/caches | Clean by producer ownership and revision after consumer retirement/acknowledgement, never by discovering and deleting an old installation. They do not become database backup inputs. |

A coherent backup contains the database and separately protected root-key
recovery material; a database dump alone cannot restore secrets.
Restore verification checks ciphertext, references, schema revision and
unresolved operations before side effects resume. Diagnostics may show record
ids, revisions, key ids, presence and safe failure categories, never secret
values or decrypted previews. Recovery and cold-start boundaries are in
[CONFIG.md](CONFIG.md); API/security rules remain in [API.md](API.md).

## Completion criteria

The switch is complete only when every durable mop reader and writer in the
matrix uses the ORM-backed domain path, generated artifacts can be recreated
without reading the old stores, and missing/corrupt database or root-key
material fails explicitly. Fresh-install, restart, concurrent mutation,
partial external-effect recovery and backup/restore checks must prove this.
This contract adds no runtime implementation and claims no deployment,
legacy migration or live-pool verification.
