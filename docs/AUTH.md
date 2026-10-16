# AUTH: как люди получают доступ к пулу

## New controller (#633, epic #623)

This section concerns the isolated Django controller, not the deployed file-backed
pool described below. It has no old-user/hash/cookie import or fallback. The issue
branch is not a rollout or an accepted epic integration. API.md remains normative.

`mop/server/access` composes standard Django User, Group, Permission and database
sessions with Principal, LoginIdentity/IdentitySource, source-owned project and
all-project memberships, and ClientOwnership pointing at Catalog's client. There
is no second Project, custom User, custom bearer table or object-permission backend.

The HTTP composition root binds these routes:

| Route | Credential and policy |
|---|---|
| `GET /api/v1/auth/csrf` | No-store CSRF cookie bootstrap; not authentication |
| `POST /api/v1/auth/login` | Authoritative password backend; CSRF even when anonymous; bounded DB-backed account/source and origin rate limits |
| `POST /api/v1/auth/logout` | CSRF-protected Django logout of this browser session only |
| `POST /api/v1/auth/token` | CLI password authentication over HTTPS; browser issuance instead requires a recently authenticated CSRF-protected session |
| `POST /api/v1/auth/token/rotate` | Current token, or CSRF/recent password-authenticated session; replaces the shared user token atomically |
| `POST /api/v1/auth/token/revoke` | Same authority; no token returned; session-based repeat is idempotent |
| `GET /api/v1/identity` | Current identity metadata only, never a credential reference or permission snapshot |
| `GET /api/v1/projects[/{uuid}]` | Catalog read use cases with the bound Access authorizer; foreign detail is 404, in-scope missing action is 403 |

Project lists filter before counts and pagination. `page_size` is 1..500 (default
100); the signed, five-minute `next_cursor` is bound to credential, current scoped
projection and installation epoch, not a stored permission grant. A changed scope
or invalid cursor returns 410 with `reset_required`, never an unfiltered next page.

DRF TokenAuthentication remains standard: one token per User, no automatic expiry
and no per-device logout. Only explicit issuance/rotation responses contain its
key. The framework Token table is the API/STORAGE credential-storage exception;
it is not AEAD/hashed token storage. Token/session records and their private binding
references require protected DB/backup access and are not registered in Admin.
CredentialBinding stores a token HMAC generation check or a reference to the actual
Django Session, never an additional plaintext token store. ActorContext contains
only immutable IDs/kind/owner/node/grant references, never token/session keys or ORM grant objects.

`token_context`, `session_context`, `current_actor` and `Access.authorize` reload
current User, identity source/subject fingerprint, credential generation, session
hash/expiry and scope. A changed external subject/provider invalidates old bindings;
a new request cannot silently rebind an existing token. Migration
`access.0002_identity_binding_subject` deliberately leaves existing binding fingerprints
empty, requiring authorized reauthentication/rotation rather than adopting a new identity.
Action permissions use a fresh User, not a retained `_perm_cache`. Every consumer
must reauthorize before execution/retry/delivery and after a wait. A context is not
a transferable JSON credential; its construction belongs to the trusted boundary.
`issue_token(user_id)` is an internal post-password-authentication hook, not a
user-ID-selected application endpoint. `bind_local_identity` is an idempotent local
maintenance hook used by initial administrator creation; it never resets a password.
`enroll_service` is the privileged local enrollment hook for a new node-bound User,
with unusable password, no staff/superuser flags and only the finite observation/
application-receipt permission allowlist. Its caller must enforce maintenance
identity and deliver the returned token through authorized enrollment, not a log.

`Access.authorize` implements Catalog's existing authorizer port. It separates
ordinary Django action permission, actual Catalog FK scope, client ownership and
principal-kind fences. `is_staff` only permits Admin login. A human superuser has
explicit global scope, not a service/grant/ownership bypass. Service users cannot
be elevated by editing User flags. Grant-kind principals fail closed until #635
binds its resolver. Catalog's immutable `permissions.ACTION_PERMISSIONS` is the
canonical conjunction of ordinary model and domain permissions; Access consumes
that map rather than treating its entries as alternatives. `set_project_limit`
is a separate action. Catalog's extra permissions are declared on Project, Node,
InstallationConfiguration and ProjectPolicySnapshot; `record_settings_application`
belongs only to InstallationConfiguration, including node-setting receipts.
Standard `view_project`/`view_node` are not declared twice. The fresh-install
catalog migration graph has one permissions leaf, `0002_action_permissions`.
Access's role/grant actions live on Principal. Later owning apps register their
CONTROL actions before the evaluator can admit them.

### Transaction-local delegated authority (#635 / #639)

`Grants.local_authority()` exposes the standard delegated resolver methods
`token_context`, `resolve_actor` and `authorize` without decrypting a prompt or
manifest. It validates live credentials, current owner/source/permissions, active
ancestors, pinned approvals and runtime bindings. Outgoing action authorization
uses the immutable normalized capability rows verified at publication/approval
and the same pure capability policy as the content-reading service. It owns a
short SQL transaction, or joins the caller's transaction; no provider/network I/O
is permitted in its approval and binding query ports.

`Grants.authorize_execution(RoleBinding)` is the narrower receiving-grant gate.
It requires an existing caller transaction and compares every actual project,
target, incarnation, role/digest, grant and human-owner reference. Ancestor Grant
rows are locked in primary-key order. Binding and approval queries must remain
SQL-only and must not acquire resource locks in the reverse order. The gate does
not authenticate the machine, authorize the initiating action, create an
execution claim or establish effect quiescence. Machine admission and the
Operations work port supply those independent checks. A human-authorized incoming
restart/message does not require the receiving role to possess the corresponding
outgoing capability.

Content-reading/preparation methods on `Grants` still perform provider I/O outside
transactions. Do not bind full `Grants.authorize` to an atomic admission callback;
use the local authority for that boundary and the separate credential runtime
for protected aggregate use cases. CreationIntent retains the exact creator
credential UUID: activation cannot select another still-valid browser/token
credential after the actual creator logged out or was revoked.

The authoritative Router respects an existing LoginIdentity before source order.
Otherwise the first knowing source decides, including wrong password, ambiguous
login, absent groups and outage; no backend fall-through adopts another identity.
LDAP users retain unusable local passwords. Direct/nested AD groups map to ordinary
Django permissions and explicit project/all-project rows. Refresh removes only the
source-owned scope and managed Group; unrelated local grants remain. LDAP I/O is
outside SQL, followed by source-revision and complete configuration CAS. Expired
validation or a changed configuration fails closed until authoritative refresh.
The configured immutable subject is `entryUUID` (default) or AD `objectGUID`, not
the DN. The DN is only the password-bind target: a rename keeps identity, while a
recreated DN or a duplicate subject cannot inherit another User's local authority.
Only LDAP invalidCredentials is a password refusal; availability and cleanup
failures propagate as sanitized unavailable errors without backend fall-through.
This validation freshness limit is not DRF token expiry.

The real ldap3 adapter accepts explicit ORM configuration and a protected bind/CA
secret-reader port. Its default reader refuses before network I/O: #637 must bind
the concrete protected secret resolver. It never consults `.env`, an old bind file
or the legacy identity provider. Fake-provider contract tests do not establish a
real LDAP deployment; real-directory acceptance and secret binding remain separate.

Legacy `identity`, `operators`, `websession`, callout/bootstrap and `server web`
consumers still belong to the deployed old release. CLI/node/gateway replacement,
operations/realtime integration, source-management Admin adapters and the remaining
reader deletion are later epic tasks, not a new-auth fallback. This code does not
kill running puppets on logout, revoke or deactivation.

## Existing file-backed release

Руководство администратора установки: как завести человека, как подключить
каталог организации, как отозвать доступ и что смотреть, когда вход не
проходит. Внутреннее устройство — субъекты, права, JWT, callout — описано в
[BUS.md](BUS.md) («Креды», «Auth callout»); здесь только то, что делает
администратор.

## Как устроен доступ

Люди берутся из провайдера личностей — и только из него. Провайдер
выбирает настройка `MOP_AUTH_PROVIDER` в `.env`: `file` (по умолчанию) —
файл операторов на сервере, который ведёт `mop server user`; `ldap` — каталог
организации (OpenLDAP или Active Directory); `file,ldap` — цепочка: люди из
файла поверх каталога. Провайдер отвечает на два вопроса: верен ли пароль
и кто этот человек — логин, имя, почта, роль и проекты.

На шину человека пускает auth callout: сервис `mop-callout` на сервере
спрашивает провайдера на каждом входе и выдаёт права по роли. Статических
учёток людей на шине нет. Люди входят только через WebSocket, то есть через
TLS-прокси сервера — так ходит `mop login` и всё, что после него. Ролей три:

| Роль | Что доступно |
|---|---|
| `admin` | весь пул плюс машинные глаголы (`disk`, узлы в сервисе кластера) |
| `user` с проектами | только названные проекты |
| `user:*` | все проекты, но не машины |

## Провайдер file: люди в файле операторов

Людей заводят командой `mop server user` на сервере — там, где лежит инвентарь
установки; на машине оператора она откажет. Делать это нужно до первого
`mop server deploy`: без единого человека в файле он откажет — на шину не вошёл бы
никто.

```
mop server user add anton --role admin --name "Антон Ермак" --email anton@example.dev
mop server user add ivan --role user --projects rugent,cloudpub --name "Иван" --email ivan@example.dev
mop server user add olga --role user --projects '*'
```

- `--role admin|user` — обязательна. `admin` проектов не принимает: у него
  весь пул. `user` принимает список через запятую или `*` — все проекты.
- `--name` и `--email` необязательны, но ими подписываются коммиты папетов
  этого человека (владелец задания, #167). Без них папет коммитит без
  личности владельца, и мастеру придётся коммитить с `git -c`.
- Пароль спрашивается дважды без эха. `--stdin` — одной строкой со
  стандартного ввода, для скриптов: `printf '%s\n' "$PW" | mop server user add ... --stdin`.

Сменить пароль и убрать человека:

```
mop server user passwd ivan            # или --stdin
mop server user delete ivan
```

На успехе команды молчат. Логин, который уже есть в файле, `add` не
заведёт второй раз.

Файл — `~/.config/mop/secrets/operators` на сервере, или путь из
`MOP_OPERATORS_FILE`. Строка на человека, поля через двоеточие, пароль —
хешем scrypt:

```
anton:admin::Антон Ермак:anton@example.dev:scrypt$16384$8$1$<соль>$<ключ>
```

Сервисы сервера (callout и глагол владельца задания) читают копию файла в
`/etc/nats/identity/`. `mop server user` обновляет её сам, если может писать в
`/etc/nats`; иначе одной строкой просит `mop server deploy` — до него новый
человек не войдёт.

## Провайдер ldap: люди в каталоге

Вход по схеме search-then-bind: служебная учётка находит запись человека по
логину, пароль проверяется bind'ом под его собственным DN. Роль и проекты —
из групп. Все настройки — в `.env`.

Обязательные:

| Настройка | Что это |
|---|---|
| `MOP_AUTH_PROVIDER=ldap` | включить провайдер (или `file,ldap`, см. ниже) |
| `MOP_LDAP_URL` | `ldaps://host[:порт]`, или `ldap://host[:порт]` вместе с `MOP_LDAP_STARTTLS=yes` |
| `MOP_LDAP_BIND_DN` | DN служебной учётки, которой mop ищет людей и группы |
| `MOP_LDAP_BIND_PASSWORD` | её пароль — секрет: только в `.env`, в плейбуки и юниты не попадает |
| `MOP_LDAP_BASE` | где искать людей |

Необязательные, с дефолтами:

| Настройка | Дефолт | Что это |
|---|---|---|
| `MOP_LDAP_LOGIN_ATTR` | `uid` | атрибут логина; в AD — `sAMAccountName` |
| `MOP_LDAP_NAME_ATTR` | `displayName,cn` | имя человека: первый непустой из списка |
| `MOP_LDAP_EMAIL_ATTR` | `mail` | почта |
| `MOP_LDAP_GROUP_BASE` | `MOP_LDAP_BASE` | где искать группы |
| `MOP_LDAP_GROUP_FILTER` | `(\|(member={dn})(uniqueMember={dn})(memberUid={login}))` | как найти группы человека: `{dn}` — его DN, `{login}` — логин |
| `MOP_LDAP_ADMIN_GROUP` | пусто | DN группы, чьи члены — `admin`; пусто — админов из каталога нет |
| `MOP_LDAP_PROJECT_GROUP` | `mop-{project}` | шаблон cn проектной группы: член `mop-rugent` — `user` на проект `rugent` |
| `MOP_LDAP_ACCESS_GROUP` | пусто | DN группы доступа ко всем проектам: её члены — `user:*` |
| `MOP_LDAP_NESTED` | пусто | пусто — только прямое членство; `ad` — с вложенными группами Active Directory |
| `MOP_LDAP_STARTTLS` | пусто | `yes` — StartTLS поверх `ldap://` |
| `MOP_LDAP_CA_FILE` | пусто | корневой сертификат каталога; пусто — системное хранилище |

Роль из групп: член `MOP_LDAP_ADMIN_GROUP` — `admin`; иначе член
`MOP_LDAP_ACCESS_GROUP` — `user:*`; иначе проекты по всем группам, чей cn
подходит под `MOP_LDAP_PROJECT_GROUP`. Человек есть в каталоге, но ни в
одной из этих групп — вход отвергается: доступа к пулу у него нет.

Шифрование обязательно: `ldap://` без `MOP_LDAP_STARTTLS=yes` mop не
примет — пароли людей шли бы по сети открытым текстом. Сертификат
каталога проверяется всегда; свой УЦ — `MOP_LDAP_CA_FILE`.

### Пример: OpenLDAP

Группы — `groupOfNames` в `ou=groups`, по группе на проект.

```
MOP_AUTH_PROVIDER=ldap
MOP_LDAP_URL=ldaps://ldap.example.com
MOP_LDAP_BIND_DN=cn=mop,ou=services,dc=example,dc=com
MOP_LDAP_BIND_PASSWORD=<пароль служебной учётки>
MOP_LDAP_BASE=ou=people,dc=example,dc=com
MOP_LDAP_GROUP_BASE=ou=groups,dc=example,dc=com
MOP_LDAP_ADMIN_GROUP=cn=mop-admins,ou=groups,dc=example,dc=com
MOP_LDAP_PROJECT_GROUP=mop-{project}
```

Член `cn=mop-rugent,ou=groups,...` получает проект `rugent`, член
`cn=mop-admins,...` — весь пул.

### Пример: Active Directory

В AD доступ обычно дают одной группой, и она вложенная — через правило
in-chain (`MOP_LDAP_NESTED=ad`) mop видит и членство через другие группы.

```
MOP_AUTH_PROVIDER=ldap
MOP_LDAP_URL=ldaps://dc.corp.example.com
MOP_LDAP_CA_FILE=/etc/ssl/certs/corp-root-ca.pem
MOP_LDAP_BIND_DN=CN=svc-mop,OU=Services,DC=corp,DC=example,DC=com
MOP_LDAP_BIND_PASSWORD=<пароль служебной учётки>
MOP_LDAP_BASE=DC=corp,DC=example,DC=com
MOP_LDAP_LOGIN_ATTR=sAMAccountName
MOP_LDAP_ACCESS_GROUP=CN=Pool Users,OU=Access,DC=corp,DC=example,DC=com
MOP_LDAP_ADMIN_GROUP=CN=Pool Admins,OU=Access,DC=corp,DC=example,DC=com
MOP_LDAP_NESTED=ad
```

Члены `Pool Users` (прямо или через вложенные группы) — `user:*`, члены
`Pool Admins` — `admin`. Проектные группы `mop-<проект>` работают и здесь,
если нужно дать кому-то только отдельные проекты.

### Цепочка file,ldap

`MOP_AUTH_PROVIDER=file,ldap` — люди из файла поверх каталога: например,
служебный админ, которого нет в AD, или человек со стороны. На каждом входе
решает первое звено, которое знает логин: логин из файла проверяется
только файлом, до каталога дело не доходит — одноимённая учётка в каталоге
не становится вторым паролем к нему. Логин, которого нет в файле, решает
каталог. Файл при этом ведёт тот же `mop server user`; при одном `ldap` он
откажет — люди живут в каталоге.

## Применение: mop server deploy

После любой смены настроек провайдера — `mop server deploy`: он раскладывает их
сервисам сервера, а пароль служебной учётки LDAP — файлом рядом с копией
людей (не в окружение юнита). Правка самих людей в файле `mop server deploy` не
требует, если `mop server user` смог обновить копию сам.

`mop server deploy` отказывает до плейбука, если:

- в файле операторов нет ни одного человека (провайдер `file`);
- логин записан в файле дважды;
- настройки LDAP неполны или неверны: нет обязательной, `ldap://` без
  StartTLS, шаблон проектной группы без `{project}`, неизвестное значение
  `MOP_LDAP_NESTED`;
- провайдер назван неизвестным именем;
- в `.env` ещё лежит `MOP_OPERATORS` (см. ниже).

### Переход со старой установки

Раньше люди задавались строкой `MOP_OPERATORS` в `.env`, а их пароли
заводил `mop server deploy`. Эта строка больше не работает, и `mop server deploy`
отказывает, пока она лежит в `.env`. Перенести людей в файл операторов с их
нынешними паролями:

```
mop server user import
```

затем удалить строку `MOP_OPERATORS` из `.env` и прогнать `mop server deploy`. Имени
и почты у перенесённых нет — их можно дописать, удалив человека и заведя
заново, или правкой строки файла. `operator.json` на машинах людей после
переноса работает без нового `mop login`: пароль тот же.

## Клиент: вход

Человек входит со своей машины, из git-рабочей копии проекта:

```
cd ~/src/project
mop login --server <адрес сервера> <логин>
```

Пароль спрашивается и сразу проверяется соединением; без терминала его
можно передать в `MOP_BUS_PASSWORD`. При первом входе закрепляется
самоподписанный сертификат TLS-прокси сервера и печатается его отпечаток —
сверьте его на сервере:

```
openssl x509 -noout -fingerprint -sha256 -in ~/.config/mop/secrets/tls.pem
```

Пароль ложится в `~/.config/mop/servers/<сервер>/` — один логин на человека
на сервер, для всех проектов (карта файлов каталога — [CONFIG.md](CONFIG.md)).
Рабочая копия запоминает только сервер и
логин (`git config mop.server`, `mop.user`, не коммитится). На самом сервере
человек входит так же — `mop login`.

## Отзыв доступа

- Файл: `mop server user delete <логин>`.
- LDAP: убрать человека из групп пула (или отключить учётку в каталоге).

Отзыв действует со следующего подключения: уже открытые соединения живут
до разрыва. Оборвать все соединения разом можно рестартом шины на сервере
(`systemctl restart nats`): каждый входит заново через callout, и отозванный
уже не войдёт — но переподключатся и все остальные.

## Когда вход не проходит

На клиенте отказ выглядит как `Authorization Violation` — без причины:
шина её входящему не сообщает. Причину пишет в журнал сервис callout на
сервере:

```
journalctl -u mop-callout -n 50
```

Строка на каждый вход: `callout: <логин> via <websocket|nats>: ok` или
`refused: <причина>`. Частые причины:

| Причина в журнале | Что делать |
|---|---|
| `<логин>: unknown login` | нет в файле (`mop server user add`) или в каталоге под `MOP_LDAP_BASE` / `MOP_LDAP_LOGIN_ATTR` |
| `<логин>: wrong password` | неверный пароль; в файле — `mop server user passwd` |
| `people connect over WebSocket only` | человек пришёл на порт шины напрямую; входить надо через `mop login` (wss через прокси) |
| `no access -- in none of the pool's LDAP groups` | человек есть в каталоге, но не в группах пула: `MOP_LDAP_ADMIN_GROUP`, `MOP_LDAP_ACCESS_GROUP`, `mop-<проект>` |
| `ambiguous login, N entries in LDAP` | логину соответствуют несколько записей: сузить `MOP_LDAP_BASE` или сменить `MOP_LDAP_LOGIN_ATTR` |
| `LDAP service bind as ... failed` | неверные `MOP_LDAP_BIND_DN` / `MOP_LDAP_BIND_PASSWORD`; после правки — `mop server deploy` |
| `LDAP ldaps://...: ...`, `LDAP StartTLS failed` | каталог недоступен или сертификат не проверился: `MOP_LDAP_CA_FILE` |

Если в журнале нет строки о входе вовсе — сервис не ответил:
`systemctl status mop-callout`. Пока он лежит, новые входы людей и папетов
отвергаются через две секунды; живые соединения, узлы и сервисы сервера это
не задевает.
