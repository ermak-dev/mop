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
project policy, credentials, user/access records and client/workspace
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
`sslkey`; backend-specific connection options beyond this finite trust schema
are selected by the foundation, not passed through arbitrary bootstrap keys.
`sslmode=disable` is available for explicitly isolated fixtures only; production
provisions verified TLS. Missing or malformed inputs fail, never select `.env`,
old profiles, a local database file or adjacent credentials.

Use separate bootstrap files **and OS service identities** for `maintenance`,
`web`, `worker` and `gateway`. Only maintenance receives `root_key`. Ordinary
web/worker/gateway processes receive their own least-privilege DB login and the
Django signing reference, not encryption-root, migration, builder, Nomad or NATS
credentials. The maintenance principal owns schema/migrations and has authorized
backup/restore access. Runtime principals need application-table CRUD and sequence
usage grants from provisioning, but no schema/table ownership, schema CREATE,
role/database administration or membership permitting elevation to such a role.
System checks reject elevated runtime principals. Foundation does not provision
roles through an HTTP request or distribute DB/bootstrap files to nodes.
#644 owns concrete grants, service units and instance-wide connection accounting.
The separately privileged builder is not an ordinary worker role.

Cold-start order is dependency setup, provisioned PostgreSQL 16/17 and trust
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
The composition runtime permits at most 8 blocking jobs and 8 async effects,
with a bounded local backlog. This does not replace durable project/node/resource
admission limits or fencing in #625/#639. Shutdown drains running work before
closing adapters/pools; a timeout reports incomplete shutdown and does not
pretend a still-running effect released its slot.

`backup --file /private/new.dump` creates a new private PostgreSQL custom-format
archive using `pg_dump`; `restore --file /private/backup.dump` uses `pg_restore`
against an **empty** configured target, with services stopped. Matching PostgreSQL
client tools of the same major must be installed by provisioning and selected
in the process PATH (CI tests matching clients 16 and 17). Mismatched tools are
refused: a newer dump tool can introduce settings an older server cannot restore. Passwords travel through a transient private passfile, not argv.
Root-key recovery material is separately protected and is not in the dump.
Restore checks schema, identity and root custody, changes the database epoch and
keeps effects disabled. #625/#639 own reconciliation before dispatch can reopen;
foundation provides no bypass to enable it. No old-data import exists.

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
