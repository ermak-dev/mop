# Configuration ownership

## Which release this describes

There are two distinct contracts:

* **Existing file-backed release:** the source currently uses `.env`,
  inventory, private credential files and runtime JSON stores. The legacy
  reference below explains that design. The precise version deployed on a
  particular installation must be checked independently; documentation does
  not establish what code is running there.
* **New Django release (#624, epic #623):** durable mop-owned configuration
  and state live exclusively in Django ORM records. This is a fresh-install
  contract, not a migration procedure for an existing installation. It is
  specified here and in [STORAGE.md](STORAGE.md), not implemented merely by
  adding these documents.

Do not remove files, change a running installation's precedence rules, rotate
credentials or point old services at a new database on the strength of this
page. There is no automatic legacy import, fallback or dual-write in the new
release. A rollout or replacement of an existing installation is an explicit
operator decision outside this documentation change.

[STORAGE.md](STORAGE.md) is the complete runtime reader/writer replacement
matrix, including configuration, identities, client bindings, secrets,
bootstrap bytes/logs, leases, grants, operations and observations.
[API.md](API.md) is the canonical API and security contract. The configuration
rules below must not create a second authentication or authorization path.

## New release: one installation authority

Django models and the ORM hold installation values, node definitions,
project policy, credentials, Actor/access records and client/workspace
bindings. Code supplies typed schemas and defaults. Accepted values and their
revisions are durable ORM state; a process's environment is not a universal
higher-precedence settings layer.

| Question | New-release authority | Consumer |
|---|---|---|
| Where is the database and how can the process open it? | Protected local process bootstrap | Django startup and local maintenance only |
| Which root key decrypts mop secrets? | Protected local root-key material outside the database | Authorized controller encryption/decryption service only |
| Which installation is this client contacting initially? | Minimal explicit address/token/trust bootstrap | Client's first authenticated API connection |
| What settings does the installation use? | Typed, revisioned ORM configuration | API/domain services, workers and configuration renderers |
| Which machine belongs to the pool and how should it run? | ORM managed-node definition and policy | Provisioning, inventory renderer, admission and agent configuration delivery |
| Which project, role and bootstrap policy is accepted? | ORM project and immutable approved snapshots | Builds, lifecycle, authorization and bootstrap services |
| What is deployed or healthy now? | Nomad, node, Git and proxy observations | Reconciliation and timestamped status views, not settings writers |
| Which server, identity and integration branch is bound to a workspace? | ORM client/workspace binding | Authenticated client context resolution |

Settings edits validate type, range, cross-field requirements and caller
permissions through the shared domain service. Store revision/provenance so a
reader can explain which configuration it used without displaying secrets.
A commit records desired state; it does not claim a service was restarted or
an external configuration was applied. Applied revision, failures and pending
work are recorded separately. Conflicting edits must not silently overwrite a
newer revision.

Deployment renders inventory, service configuration, specs and consumer files
from an accepted ORM revision. It does not seed empty records from a leftover
`.env` or infer settings from generated files. A file edited on a node is not
a settings change; reconciliation can replace it from desired state and must
report an unapplied revision honestly. No database/root credentials go to
nodes or puppets. Private consumer credentials remain purpose-scoped, with
no secrets in a Nomad job spec or ordinary systemd environment output.

External integrations are not brought inside Django by renaming their files.
Git owns source history and actual worktrees; Nomad owns allocations and
scheduler/ACL state; CLIProxyAPI owns provider credentials and internal
configuration/runtime persistence. Mop owns the integration settings and
intent it manages, records accepted input revisions and uses adapters to
apply/observe external effects. See STORAGE for the exact boundary.

## Protected process bootstrap

The database cannot contain the only copy of the information required to open
it. The encryption root key cannot be stored in the database it protects.
These are explicit trust roots, not exceptions permitting arbitrary settings
to remain in `.env`.

The controller's protected local bootstrap contains only what is necessary
to initialize Django and reach the configured database: backend/connection
coordinates, database authentication and required database TLS trust, the
installation identity needed for validation, and a reference to protected
root-key material. Any additional pre-database cryptographic material must
have a documented startup need. Exact setting names and management-command
spellings belong to the implementation; this contract does not invent a
working command or a new environment variable.

Bootstrap and root-key files use restricted ownership and permissions
(private directories, ordinarily `0700`, secret files `0600` or an equivalent
service-manager credential boundary). They are outside the repository,
web-served directories, database backups and generated node/body files.
Only the controller service account and explicitly authorized local operator
can access them. Do not put secret values into command-line arguments,
version control, logs, diagnostics, normal unit `Environment=` lines or
world-readable configuration. A protected service-manager injection may
transport bootstrap material, but must not revive the old blanket
`environment > file > database` precedence.

Every other persistent mop setting, including service addresses, ports,
provider policy, resource limits, node overrides, selected client bindings,
secret values and accepted project configuration, is ORM-owned. A root-key
reference is not a path to a general secret-directory fallback. Missing,
unreadable, malformed or wrong-installation bootstrap must give a safe,
specific failure. Missing tables, incompatible schema and failed decryption
are distinct from a valid empty installation; none triggers file discovery,
credential regeneration or an in-memory-only mode.

## Foundation entrypoints (#632)

The isolated new controller lives in `mop/server/controller`; it does not replace
any deployed old service merely by being present. Run its commands through
`mop server controller`, not `manage.py`, `runserver`, or old `server web`.
The dispatcher bypasses old context/environment initialization for this command.

`setup --venv /absolute/new/path` is the stdlib-only source-tree installer. It
installs the package's declared `[server]` extra into that new environment and
checks its framework imports. The ordinary package install and node dependency
list do not contain Django, Channels, DRF or psycopg. Use that environment's `mop`
for subsequent controller commands. Setup installs software only, never creates
trust roots, connects to an installation, runs migrations or starts a service.

`MOP_CONTROLLER_BOOTSTRAP` is the one explicit absolute path to a private JSON
file. It is a startup selector, not a general environment-over-ORM precedence
rule. The bootstrap and referenced key files must be regular, owned by the
process account, inaccessible to group/others, in private owned directories;
symlinks are refused. Provision these outside the repository and served files.
The finite schema is:

```json
{
  "installation_id": "new-installation-identity",
  "role": "maintenance",
  "database": {
    "ENGINE": "django.db.backends.postgresql",
    "HOST": "database.example.invalid",
    "PORT": "5432",
    "NAME": "new_controller_database",
    "USER": "schema_maintenance_principal",
    "PASSWORD": "<provided privately, never in argv>",
    "OPTIONS": {
      "sslmode": "verify-full",
      "sslrootcert": "/private/trust/database-ca.pem"
    }
  },
  "root_key": "/private/controller/encryption-root.key",
  "django_key": "/private/controller/django-signing.key"
}
```

The encryption root contains at least 32 bytes. The separate Django signing key
contains at least 50 ASCII bytes of cryptographically random material; Django
needs it before DB-backed authentication starts. Do not use the illustrative
strings above as credentials. The optional TLS options are `sslcert` and
`sslkey`, supplied together when selecting a client identity. Every supplied TLS
reference must be an explicit absolute regular file without symlinks, owned by
root or the process account, with no group/other write permission. Public CA and
certificate files may be `0644`; a private client key must be `0600` or `0400`.
Every ancestor must also be owned by root or the process account and protected
against untrusted replacement. A trusted sticky directory (such as `/tmp`)
may contain a trusted-owned descendant; an untrusted owner or a non-sticky
writable directory is refused. Bootstrap/root/Django key files retain their
stricter private-directory and process-owner requirements.

`verify-full` requires an explicit protected `sslrootcert`, never ambient
`~/.postgresql/root.crt` discovery. The libpq `system` sentinel is not supported:
this foundation accepts validated filesystem references, not version-dependent
system-trust selectors. Validation does not parse PEM contents or copy trust
material; provision valid certificates and keep the protected paths stable for
runtime and native backup/restore consumers. Backend-specific connection options
beyond this finite trust schema are selected by the foundation, not passed through
arbitrary bootstrap keys.
`sslmode=disable` is available for explicitly isolated fixtures only; production
provisions verified TLS. Missing or malformed inputs fail, never select `.env`,
old profiles, a local database file or adjacent credentials.

Use separate bootstrap files **and OS service identities** for `maintenance`,
`credential`, `web`, `worker` and `gateway`. Maintenance receives `root_key` for
initialization and authorized backup/restore trust checks. Only `credential` can
compose the AEAD provider and run secret application use cases at runtime.
Ordinary web/worker/gateway processes receive their own least-privilege DB login
and Django signing reference, not encryption-root, migration, builder, Nomad or
NATS credentials. The maintenance principal owns schema/migrations and has
authorized backup/restore access. The credential bootstrap requires protected
root and Django signing references plus its own restricted DB login; it has no
maintenance, schema, migration or backup privilege. Like other runtime principals,
it needs application-table CRUD and sequence usage grants from provisioning, but
no schema/table ownership, schema CREATE, role/database administration or
membership permitting elevation to such a role. System checks reject elevated
runtime principals, including `credential`. Foundation does not provision roles
through an HTTP request or distribute DB/bootstrap files to nodes.
#644 owns concrete grants, service units and instance-wide connection accounting.
The separately privileged builder is not an ordinary worker role.

#644 provisions three ordinary runtime LOGINs (`web`, `worker`, `gateway`),
each with PostgreSQL `CONNECTION LIMIT 8`: at most 24 connections under these
identities across their processes, not a group-role limit. A distinct maintenance
LOGIN has another limit of 8. The target database limit of 32 is an upper bound,
not a reservation of eight maintenance slots. Verification rejects incoming and
outgoing membership for all four identities (including transitive INHERIT/SET
ROLE paths), any additional ordinary LOGIN with CONNECT to the target, and
unconfigured object/default ACL grantees. This deliberately rejects unsupported
ACL topologies rather than silently calculating a fictitious aggregate.

This is accounting for the managed target's ordinary logins, not all PostgreSQL
sessions: external superusers and consumers of other databases are outside that
scope and can exhaust shared server capacity. Operators must provision adequate
server capacity and restrict external administrative access; a database limit
alone cannot guarantee availability. The current installer does not yet provision the separate credential-service DB
bootstrap or LOGIN; that #669 production binding remains unavailable. Builder gets no runtime DB login. The separately privileged builder is not an
ordinary worker role.

Ordinary public application tables allow SELECT/INSERT/UPDATE/DELETE; sequences
allow USAGE/SELECT, never UPDATE or grant options. Verification checks effective
existing table, column and sequence privileges, plus the union of global and
schema-local defaults for future objects (including PUBLIC). PUBLIC and
unconfigured grantees are refused conservatively. Trust tables
`controller_restoreguard` and `controller_installation` are exceptions: runtime
gets SELECT and UPDATE(id) only, for Django row locks. Their singleton constraints
make id changes fail. Only maintenance may change state/generation, installation
identity, root evidence, epoch or effects, or insert/delete the singleton. The
schema ACL migration removes ordinary default CRUD from these exceptions before
schema preparation completes; changing defaults later does not regrant existing
tables. Native restore must preserve this exception when recreating trust tables.
`database-check` is read-only and never repairs an external database: unsafe ACLs
require an explicit operator provisioning/schema-maintenance action. Runtime units
and full installation apply remain blocked on the missing Root consumers.

### Isolated credential runtime (#669, Secret core #637)

`mop server credentials --bootstrap /private/credential/bootstrap.json
--socket /run/private-credential/service.sock --key-id root-v1 --peer-uid UID`
uses only explicit protected bootstrap and a local Unix socket. This command does
not start any deployed old service, provision accounts or install units. It checks
foundation schema/restore/installation readiness and runtime DB privileges before
constructing the AEAD provider. Protected bootstrap loading and installation
verification can read the root earlier; this is not a pre-root-read DB gate.
Key identifiers must remain stable for retained envelopes; this entrypoint supports one retained root identifier, not
automatic root rotation. Missing historical keys fail closed.

The socket parent must already exist, be owned by the credential service UID,
have no group/other writers, and have a trusted non-symlink ancestor chain.
Startup refuses existing endpoints, including stale sockets; an operator must
verify stopped ownership before local repair. The endpoint is `0600` by default.
For distinct frontend OS accounts, provisioning can give a dedicated group
traversal on the service directory (for example `0750`), and select that service
account's group with `--socket-group GID` for socket `0660`. Frontend accounts must
not be directory writers or able to read the bootstrap/root. No `0777` directory,
public socket, network listener or automatic stale-socket deletion is provided.

Every connection must pass Linux `SO_PEERCRED` against explicit repeatable
`--peer-uid` values (empty means deny all), then authenticate a privately
forwarded **current standard Access token or Django session credential**. This
is an internal trusted-frontend boundary, not a browser endpoint: existing HTTP
login/CSRF/session checks remain the frontend's responsibility. OS peer identity
does not select a user or grant. The service constructs ActorContext through
Access, never from caller owner/project/actor claims. No new bearer, JWT, shared
service password, credential file or persisted socket session is introduced.

Rootless `mop.common.credential_client.CredentialClient(path, server_uid=UID)` is stdlib
only. It verifies the service peer UID and protected endpoint and exposes typed
`create`, `stage`, `update`, `issue_addressed`, `issue_rotation`,
`publish_bundle`, `read_bundle`, `approve_bundle`, `reserve_grant`,
`provision_grant`, `publish_preparation`, `read_preparation` and
`approve_preparation` methods. Forward a `mop.common.credentials.Credential('token'|'session', value)`
privately per call; neither it nor returned secret bytes have a value-bearing
repr. Calls must be outside SQL transactions; async frontends use their owned
bounded blocking runtime. No retries are performed: lost responses or timeouts of
mutations mean unknown outcome and require reconciliation, not blind replay.
The finite version-1 length-prefixed JSON protocol accepts at most 2 MiB per
frame and 1 MiB per value, one command per connection, at most eight accepted
connections and eight DB/effect tasks. Reads, writes and command waits have a
five-second default deadline (hard maximum ten seconds); timed-out DB tasks stay
owned by Runtime until they finish, and shutdown drains them. No access log or
request-body logging exists.

`create`/`stage`/`update` call the real SecretStore with current Access action and
scope checks before/after AEAD, and actual relational AAD, version/CAS and nonce
constraints. The runtime additionally pins installation identity, epoch, root
fingerprint, effects setting and restore generation around use cases and crypto
waits, using ORM-only checks inside SQL sections. It never reads a root file or
calls the provider inside an authorization transaction. Installation-scoped
credential mutation is explicitly unbound and denied: project-secret permission
does not authorize changing Nomad, signer or installation integration credentials.
Their finite application-level composition must be accepted separately.
`read_bundle(bundle_id)` calls public `Grants.read_bundle`: retained
bytes, exact owner/project, current permission, immutable bundle/digest and
normalized capability checks, not a caller-selected ciphertext/context oracle.
There is no raw `RetainedSecrets.prepare/commit` socket pair: passing an opaque
prepared envelope back would widen the interface and putting socket I/O inside
an aggregate SQL transaction is forbidden.

Grants' current binding query uses the public SQL-only
`Assignments.current(project_id, target_id)` projection: exact project, target,
incarnation, owner, role, bundle digest and grant. `provision_grant` calls the real
Grants owner and can retain a new credential without returning its token. A live
mismatched assignment is refused before provider work and checked again before
the retained commit. Bundles must be published through Grants, which seals their
complete normalized sets; manually populated approval rows are not authority.

Activation and transport retirement remain explicitly unbound: a current read
projection is not a trusted activation or retirement receipt. Addressed and
rotation issuance also remain denied by `AwaitAssignment`, even for a permitted
human. They require an authenticated enrolled node, current assignment, exact
role/digest/incarnation, active grant/ancestors, current owner rights and pinned
required-name set, not merely the new current-binding adapter. The public
SecretStore checks full Secrets delivery action/scope before/after crypto; a
binding alone is not permission. No node-addressed proof or receipt is fabricated.

The implemented rootless aggregate boundary publishes/reads exact encrypted role
and Preparation bytes, approves them, and reserves grants with the real
`Preparation.approval_query()` proof pinned by Grants into its immutable intent.
Provider work stays outside SQL; current actor authorization inside SQL uses
`Grants.local_authority()`, not the decrypting `Grants.authorize` chain.
The retained-secret authority keeps its domain-owner checks and adds the root /
epoch / restore-generation fence at prepare, aggregate commit and read release.
A changed root during crypto cannot merely fail after committing a stale bundle.
Receipts contain metadata and immutable references, never the grant token.

This initial #669 stage does not wire the production web/worker HTTP routes,
node-addressed delivery, #643 admissions, or anonymous LDAP service bind / scoped
Controller integration consumers. Those non-human consumers need explicit
purpose plus OS-peer authority and a concrete current-resource query; forwarding
an arbitrary human owner ID as actor authority is not permitted. They remain
closed rather than weakening admission or giving ordinary roles a CryptoProvider.
#644 separately owns service identities, units and DB provisioning.

This adapter does not provide a role-create admission workflow. Public
`Assignments.create` currently takes a grant ID while generating the target ID;
`Grants.reserve` takes the target/incarnation while generating the grant ID.
Connecting those identities for a new target still needs an accepted owning
admission use case. The current Operations vertical is node drain/enable, not a
generic role-create/stage handler. Socket tests with persisted matching
assignments prove the binding checks, not that missing end-to-end workflow.
Grants also requires the registered `sessions.create_puppet` permission; the
current execution app installs `execution.*`, not that role-create permission.
The socket fixture explicitly supplies it. Production admission must own its
registration rather than treating a superuser or caller-selected owner as a
substitute for a missing capability.

Owned source-only socket checks can exercise actual distinct OS UIDs with
`mop dev controller --postgres 17 --check credential_service --cross-uid`
on Python 3.12. Only that explicit fixture runs its test process as root inside its
new isolated container; the default CI suite remains unprivileged. This creates
no live pool service or host account and passes no installation inputs.

Cold-start acceptance uses Python 3.12 + PostgreSQL 17 only. Its order is dependency
setup, provisioned PostgreSQL 17 and trust
roots, `migrate`, `initialize`, `admin --username name`, `check`, then `serve` and
`worker` under their restricted principals. `admin` reads and confirms the
password privately; it only creates the first administrator and refuses to reset
an existing account. `initialize` binds a singleton installation identity/root
fingerprint without resetting it on repetition. `migrations-check` checks model
changes; `check` also refuses unapplied schema migrations or a mismatched
installation. These maintenance paths use the configured DB directly with HTTP
and NATS stopped. They are not a general domain-repair console; domain repair
and audit services connect in their owning epic tasks.

`serve --host bind-address --port port --hostname public-name --tls-cert path
--tls-key path` starts one bounded production Uvicorn ASGI process, not Django's
development server. `--hostname` is repeatable and distinct from the bind address;
wildcards are refused. A non-loopback listener requires native HTTPS, the key is
private, and untrusted forwarding headers are disabled. Loopback HTTP redirects
to HTTPS rather than accepting authenticated cleartext requests. Deployment
renders these process arguments from accepted ORM configuration in #644; they
are not another persistent settings registry. Standard Auth/Admin/Sessions and
DRF token infrastructure are installed; token rows are not exposed in Admin.
The only foundation API query is authenticated `GET /api/v1/health`. There are
no temporary admission/operations queues, anonymous WS endpoints or domain apps.
The worker entrypoint owns lifecycle; #625 binds its durable handlers.

The initial process DB pool has at most 8 connections (zero persistent
thread-local connections), with 5 s connect/query, 2 s lock and 10 s idle-in-
transaction deadlines. Application services use short synchronous atomic blocks;
the maintenance `short_transaction` helper checks its 10 s budget before COMMIT.
The composition runtime permits at most 8 blocking I/O jobs, 8 synchronous
DB jobs in a separate executor, and 8 async effects, with a bounded local
backlog. Slow blocking adapters cannot occupy the DB threads needed for
queries, claim/result recording or renewal. This does not replace durable project/node/resource
admission limits or fencing in #625/#639. Shutdown drains running work before
closing adapters/pools; a timeout reports incomplete shutdown and does not
pretend a still-running effect released its slot.

`backup --file /private/new.dump` creates a new private PostgreSQL custom-format
archive using `pg_dump`; `restore --file /private/backup.dump` uses `pg_restore`
against an **empty** configured target, with services stopped. A completely empty
target is provisioned by standard Django migrations; an already migrated but
uninitialized controller target is also accepted. Existing installation/domain
data or unrelated tables are refused. Matching PostgreSQL client tools of the
same major must be installed by provisioning and selected in the process PATH
(the required CI verifies PostgreSQL 17 clients and server). Mismatched tools are refused: a newer dump
tool can introduce settings an older server cannot restore. Passwords travel
through a transient private passfile, not argv. Root-key recovery material is
separately protected and is not in the dump. Treat archives as authorized
maintenance input, not uploads from untrusted API users: PostgreSQL archives can
execute database code under the maintenance principal.

The ORM-owned `RestoreGuard` singleton holds current target maintenance state and
an independent generation, with no foreign key into archived tables. Its entire
table (schema and data) is deliberately excluded from every backup. This is not
legacy import, a file marker or a second installation/configuration authority.
Standard migrations create its uninitialized row; only initialization of a truly
new installation can make that row ready. A missing guard row/table is a refusal,
not permission for `initialize` to recreate it around existing data.

Restore requires autocommit and refuses an ambient application transaction
before filesystem/tool work: its guard arm must be a real COMMIT, not a caller's
savepoint. It commits a new, dirty guard generation **before** invoking archive tools.
The archive's table of contents is checked for forbidden guard entries before
execution; the public schema itself is not dropped. Archive migration history
must exactly match the controller's migrated target. Older/newer or otherwise
incompatible schema archives are explicitly refused, not restored under a
fabricated current migration ledger. Supporting schema-upgrading restore later
requires an explicit recovery procedure; ordinary `migrate`/`initialize` do not
clear a failed restore. No ad hoc schema creation or migration faking is used.

After archive COMMIT the guard still denies startup, even though the archive may
contain an old epoch with effects enabled. Restore commits a fresh installation
epoch and disables effects, verifies schema, identity and root custody, then
clears only the same guard generation. Crashes or failures before that completion
commit, including before/during/after archive COMMIT, failed validation and stale
restorers leave it dirty across process restarts. A lost completion-COMMIT reply
is an unknown outcome: the target may already be valid in its new epoch, still
with effects disabled. Check it rather than assuming rollback or enabling effects.
Keep services stopped and use a fresh empty target for a new restore attempt;
do not delete/reset the guard as a repair shortcut. Successful restore still
leaves effects disabled. #625/#639 own reconciliation before
dispatch can reopen; foundation provides no bypass to enable it. No old-data
import exists.

`runtime.require_restore_ready(using='default', lock=False)` is the cheap
single-row readiness query for future admission/claim consumers. The composition
root injects this check into domain services; domain modules must not import the
composition runtime back into their own layer. It returns the
current guard generation or refuses missing/uninitialized/dirty state. With
`lock=True` it must run inside the caller's short SQL transaction, before
project/resource locks, so a restore cannot arm concurrently with that admission
or claim commit. #644 must provision runtime SELECT on the guard and only the
column UPDATE privilege needed for row locking (the constrained singleton `id`),
not writes to its state/generation or INSERT/DELETE. Guard writes belong to
maintenance. `verify_installation` checks the guard at startup; health snapshots
also check it. #625/#639 must call the cheap API in admission and claim
transactions, bind/check the returned generation and current installation epoch,
and revalidate before external dispatch and after waits. Readiness does not
mean `effects_enabled`, authorize a caller, reconcile claims or stop an already
running external effect. No external I/O runs while holding the guard row lock;
restore still requires quiescent services. These are required integration hooks,
not a claim that the foundation implements #625.

## Initial client connection is not a local settings registry

A remote client needs a server address and an explicit TLS trust basis when
normal system trust is insufficient. It obtains its first token through API.md's
Django-password-authenticated issuance endpoint, or receives an enrolled service
credential through authorized local provisioning; a token is not a prerequisite
for password login. This minimal local bootstrap
is unavoidable connection material, not a replacement `connection.json`
containing all server settings or a general environment override system.
It contains no Nomad token, database password, encryption root key, LDAP bind
secret, proxy management key or another principal's credential.

After authentication the client resolves its authorized server/identity and
workspace/integration-branch binding from ORM-backed API records. Binding
creation and changes use that API and its access checks. Remote clients never
receive direct database access. Explicit one-command selection is request
context; it must not silently rewrite the durable binding or gain rights from
a claimed username. Cached responses are revisioned, non-authoritative views,
not offline configuration or authorization fallback.

Old `git config mop.server/mop.user/mop.branch`, `MOP_SERVER_LAN`,
`MOP_BUS_USER`, `MOP_BRANCH`, joined-server directory scanning and legacy
connection files are not durable binding authorities in the new release.
An unavailable API is an explicit connection failure for a remote client,
not permission to choose a neighboring server or use a stale local profile.
The lifecycle, scope and protection of the initial credential follow API.md.

## Cold boot and local repair

First installation and recovery must work with the HTTP server stopped. A
controller-local maintenance entry point initializes Django directly, opens
the same database through the ORM and invokes the same validated domain
services. It must not call the public HTTP API, an internal HTTP endpoint or
the commandlets as subprocesses. Operator identity and authorization for this
privileged path are defined by API.md; local repair is not anonymous remote
administration or arbitrary SQL access.

The required lifecycle is:

1. **Provision trust roots locally.** Supply protected database bootstrap and
   create/provision the root key outside the database. Do not read an old
   installation's files to fill missing values. First initialization is
   explicitly requested and distinguishable from repairing an existing
   installation; absence of a database or key is not automatic consent to
   create replacements.
2. **Initialize the new schema and installation.** Run Django migrations and
   validated initial domain setup through the local maintenance path. Record
   installation identity, required settings and initial authorized operator
   without relying on a healthy HTTP listener. Repeating setup must not reset
   identities, rotate secrets or overwrite an initialized installation.
3. **Validate before serving or dispatching.** Check schema compatibility,
   bootstrap/installation identity and decryptability of required secret
   versions. Build effective configuration from ORM; only then enable normal
   HTTP, workers and external-effect dispatch. Diagnostics expose presence,
   revisions and safe error categories, never secret contents.
4. **Repair without the broken endpoint.** A bad public address, TLS setting,
   provider configuration or revoked remote login must be inspectable and
   correctable through authorized local ORM/domain maintenance. Record actor,
   reason and revision/audit changes just as for an API mutation. Validation,
   encryption and access-policy invariants still apply; direct row editing
   that bypasses them is not the repair contract.
5. **Restore coherently after loss.** Recover a consistent database backup and
   its separately protected root-key material, verify the identity/schema,
   decryptability and references, then reconcile unresolved operations before
   resuming effects. An unreachable database requires database/bootstrap
   repair first; a lost root key cannot be repaired by reading ciphertext or
   regenerating a key under the same identifier. Report unrecoverable secrets
   explicitly, and re-provision only under an authorized recovery decision.

This is an implementation requirement, not a set of executable instructions
for the currently deployed old release. Concrete installation/maintenance
commands must be documented and verified when implemented. Do not attempt
the sequence against an existing file-backed service.

## Existing file-backed release: reference only

This section describes the pre-Django architecture visible in the source. It
is neither a source list for automatic import nor a new-release operations
manual. In the table, the controller is the installation server and Ansible
controller; the master is the operator's client machine; a node runs pool
bodies. Each `~` belongs to that process's account.

### Setting resolution and ownership

The existing `mop/common/config.py` resolves normal settings as context (for
context-scoped keys), process environment, node `node.env`, controller `.env`,
then code defaults. Client mode omits the installation `.env`.
`config.SETTINGS` defines defaults, `REQUIRED` declares required installation
inputs, `NODE_SCOPED` controls delivery to nodes, and `SERVER_SCOPED` controls
service-unit settings. Inventory gives machine/group overrides. The deployment
must not copy the installation `.env` to nodes: it can contain tracker and
other installation credentials.

| Existing store | Writer/owner | Readers and role |
|---|---|---|
| Controller clone `.env` | Operator; setting-edit logic where used | Dispatcher/config resolution, server commands and deployment input |
| `inventory.yaml` | Operator | Deployment, machine/group settings, inventory membership checks |
| Controller `sandbox.yaml`, project `.mop/sandbox.yaml` | Installation/project source owners | Image/build and resource-request collection; not per-start bootstrap policy |
| Node `~/.config/mop/node.env` | Deployment from inventory and declared node-scoped settings | Agent and node drivers; manual edits are overwritten |
| Node/body `secrets.env` | Cluster proxy-key distributor and node/body delivery | Wrapper's proxy key and optional CA material; not general installation settings |

### Credentials and state stores

| Existing store | Writer → reader | Boundary |
|---|---|---|
| Controller `~/.config/mop/secrets/` passwords and private keys | Deployment/key provisioning → NATS/proxy rendering and controller services | Private credentials; generated passwords are reused, not rotated merely by a normal deployment. Losing the directory is a credential-loss incident, not a harmless cache miss. |
| `secrets/operators`, configured operator file and `/etc/nats/identity/` copies | User administration/deployment → file identity provider, callout and web login | Local users/password hashes; LDAP is a separate provider, with a protected bind password |
| `/etc/nats/base-users.json`, `users.conf`, `callout.conf` and callout seeds | Deployment plus `natsconf`/callout key setup → NATS/callout | Generated machine/project access configuration, not a human password database |
| `servers/<address>/connection.json` on the master | Login → `creds.snapshot`, bus connection, session environment, models and diagnose | Whole human connection profile; the old human `operator.json` + `client.json` pair is no longer read after #662. Broken new profiles fail rather than fall back. |
| Service `servers/<address>/operator.json`, optional `client.json` | Deployment → server machine connection consumers | Service machine identity remains distinct from human login; do not merge their credentials |
| `servers/<address>/tls.pem` | Login/credential collection/deployment → TLS consumers | Trust pin or CA material; separate from credential profile rotation |
| Controller `servers/<address>/bootstrap.json` and Nomad's own bootstrap file | Nomad ACL bootstrap/deployment copy → controller Nomad adapter | Nomad management token stays on the controller, never distributed to masters/nodes |
| `projects.json` | Cluster project mutation → registry/admission/list/deployment | Versioned project snapshot is authoritative; limits travel with project records. The older `projects`/`limits.json` pair is not a new-release fallback or import source. |
| `project-asks.json`, `inventory-hosts.json` | Deployment → spec resource requests and cluster forget guard | Derived deployment files, refreshed by deployment rather than a live project edit |
| `puppets/nats-puppet-*.pass` | Server `natsconf` → project credential/bootstrap readers where used | Project credential store is separate from master credentials; role grants have their own credentials |
| `souls/registry.json`, grant passwords and pinned bundle files | Grant lifecycle service → callout, cluster and bootstrap resolvers | Role binding, incarnation, request reservation, approval and revocation authority; not interchangeable with a self-declared role name |
| `bootstrap/<puppet>-tasks.yml`, `-vars.yml`, `-sent.json`, `-approved.json` | Human lifecycle requests and approved inheritance → bootstrap/registration | Accepted per-puppet policy, including explicit empty approval; not overwritten by project registration or ordinary deployment |
| Bootstrap result/progress JSON and progress logs | Bootstrap runner → polling, web progress and give-up logic | Execution evidence associated with registration and allocation; may contain sensitive output |
| `project-secrets/<project>/files/`, `vars.env` | Cluster secret operations → bootstrap and body delivery | Project secret values, role allowlist filtering and restricted delivery, not installation settings |
| `landing.json` | Cluster landing operation → landing arbitration | Runtime ownership survives service restart; deployment must not reset it |
| `proxy-key.json`, `proxy-rotation.json` | Proxy distribution/rotation → status and recovery | Applied-key fingerprint and private rotation checkpoint are separate from the actual key |
| `sessions.json` | Web login/logout → session authentication | Existing persistent web sessions; this is not the new ORM session contract |
| Clone `.git/mop-owner` | Agent ownership handling → lease/safety checks and Git identity installation | Ownership is not inferred from a silent session or clean-looking dashboard row |
| Git config `mop.server`, `mop.user`, `mop.branch` | Login/operator → command context | Existing client selection layer, not the new ORM binding design |
| Node `bus.json`, body bus credentials, role bundles/ready receipts, generated hooks and MCP configuration | Deployment/driver/agent/bootstrap → execution consumers | Distinct machine/project/grant scopes; no universal credential file |
| `.local/state/mop/turns`, `models`, runtime session registry and transcripts | Hooks/session runtime → agent/roster/usage/history | Observations and runtime-owned history, not installation desired state |
| Admin operations, per-target lanes, terminal registries and web event rings in memory | HTTP adapter/workers/terminal/collector → live clients | No durable operation queue in current `adminapi`; historical `ops.jsonl` is not an active store. Journald and NATS events do not make an in-memory operation result durable. |
| `llm-proxy/config.yaml`, provider auth directory and proxy logs | Initial provisioning then CLIProxyAPI/operator management → proxy | External service persistence; repeated provisioning must not erase provider management changes |

Existing secret directories and files normally use `0700`/`0600`; public
configuration and trust certificates can be readable without exposing private
keys. Refer to [AUTH.md](AUTH.md), [BOOTSTRAP.md](BOOTSTRAP.md),
[CLUSTER.md](CLUSTER.md), [PROXY.md](PROXY.md), [SOUL.md](SOUL.md) and
[CLI.md](CLI.md) for the existing protocols and command reference. Historical
#531 transition/export behavior is not the transition plan for #623; reverting
code is not a database migration or a credential/data rollback.

The legacy mechanisms above remain facts about the old release until an
explicit replacement. They do not authorize preserving any of those files as
a runtime reader/writer authority in the fresh Django installation.
