# HOST containment supervisor

Node-layer physical boundary #719 для нового controller release. Это не
SessionApi, readiness receipt, producer registry или новый владелец business
state. `body_launch.py`, `native_observer.py`, installer и существующий пул не
подключены к этому модулю. Production capability остаётся unbound до отдельной
установки и composition/acceptance задачи. PVE не наследует HOST acceptance.

## Authority

Три разные OS authority:

- Node service UID хранит свой HTTP bearer в собственном private tree. До
  каждого socket request `Client` вызывает current server admission guard,
  проверяет pinned Work address/expiry и повторяет local fence после await.
  Это тот же `EffectGuard`, что у execution boundary. Composition обязана
  связать client и guard с одним admission; scope не заменяет claim.
- Root supervisor принимает только точный installed node-service UID через
  Linux `SO_PEERCRED`. Даже root peer не исключение. Caller JSON не выбирает
  UID/GID, runner, root path, credentials или systemd properties. Work DTO,
  HTTP bearer, DB/rootCrypto/Nomad/proxy-management/human credentials в
  протоколе отсутствуют. Supervisor не читает `.env` или credential store.
- Dedicated body UID/GID отличаются от node service UID, не равны root.
  Project/native code исполняется только от body UID, не внутри node service.
  Body не имеет права подключаться к socket, читать node-private tree или
  перемещать процессы через cgroup filesystem.

`Installed` — trusted root-installed descriptor, не DTO из socket. Он содержит
только numeric UID/GID, fixed runner, body root и node-private root. Executable
и все его ancestors должны быть root-owned и не writable group/other; identity
inode/mtime/size фиксируется на время daemon. Interpreter/dependencies runner
также являются установленной trusted частью, не проектным кодом. Directory
ancestors production socket/body root/node-private parent immutable root-owned;
node-private leaf принадлежит node UID и имеет private permissions.

Root daemon entry — `host_scope_supervisor.serve(installed, socket_path, stop)`.
Будущий installer должен запускать его отдельно, без server/DB/rootCrypto/
Nomad/proxy-management/user credentials в argv/env/files/FD и без наследования
node bearer. Этот task не меняет service units/deploy/installer. Node UID не
получает root shell, sudo, systemd D-Bus rights или generic exec/write API.

Dedicated body account должен быть non-login, без sudo/polkit/cron/at rights,
linger/user manager, privileged groups и любых альтернативных launch services.
Его нельзя переиспользовать как человеческий или node-service account. Один
body UID — одна consumer trust domain: cgroup не изолирует secrets двух тел,
запущенных с одинаковым UID. Разным consumer security domains нужны разные
root-installed descriptors/body accounts и раздельный provisioning; этот port
не является межпроектной filesystem/ptrace sandbox. Наличие
внешнего сервиса, позволяющего этому UID запускать работу вне owned subtree,
нарушает install contract; флаг Delegate=no не исправляет такую установку.
В unit дополнительно закрыты `/run/user`, system D-Bus/systemd private socket,
node-private tree; readonly cgroup mount, empty capability sets,
`NoNewPrivileges`, `RestrictSUIDSGID` и `RestrictNamespaces` действуют до exec.

## Finite protocol

Один Unix connection: request newline frame, один response, receipt ACK.
Frames не больше 4096 bytes, duplicate keys/unknown fields/future versions
отказывают. Всего три действия:

- `start(scope, guard)` — только fixed installed runner, argv
  `[runner, "--scope", scope.unit]`. Existing unit/collision или повторный
  start в данном daemon не повторяет effect. Node обязан дать новый launch UUID
  для новой lifetime, а не восстанавливать старый start.
- `inspect(reference, guard)` — точная physical identity: полный scope,
  nonzero systemd InvocationID и kernel boot ID. Неверный token/epoch/unit
  отказывает. Это physical observation, не native/session readiness.
- `retire(reference, guard)` — повторная проверка actual unit/kernel facts,
  pinned cgroup directory inode, затем только `cgroup.kill = 1` в этом inode
  и чтение `cgroup.events: populated 0`. Не PID kill, не kill-pane/process group,
  не prefix-based systemctl stop.

Scope состоит из шести canonical UUID: installation epoch, node ID,
executor epoch, target ID, incarnation ID, launch ID. Unit name — reversible
lowercase base32 всех 96 UUID bytes, `mop-host-<154 chars>.service`, без
сокращённого hash/sidecar lookup. Так generated PrivateTmp component остаётся
короче Linux NAME_MAX. Systemd slice `mop-host.slice` имеет parent `mop.slice`;
точный cgroup path — `/mop.slice/mop-host.slice/<unit>`.

`Snapshot` содержит только physical reference и boolean populated. Не содержит
ready flag, model, session ID, allocation anchor или business status. Reference
не server authorization token. Kernel/unit facts не превращаются в ORM registry.

## Production OS adapter

`SystemdCgroupAdapter` требует root, systemd system manager и unified writable
cgroup-v2 с `cgroup.kill`. Root/body/runtime paths должны быть локальными
trusted filesystems, не untrusted FUSE/NFS destinations. Никакого fallback на
PID, process group, pane, исчезнувший ready file или старый allocation.

Start использует только установленный `systemd-run`, фиксированный набор
properties, `Type=exec`, `Restart=no`, `RemainAfterExit=yes`, `KillMode=control-group`,
`TasksMax=128`, `Delegate=no`, readonly system/home/cgroup mounts и private
runtime devices/tmp. Service manager environment допускает только явно
разрешённые locale/PATH values; неизвестная переменная блокирует start, а не
переезжает к body. Subprocess supervisor запускаются со своим минимальным env,
без наследования credential variables.

Body directory `<body_root>/<launch UUID>` создаёт отдельный trusted provisioning
owner; scope API не создаёт и не пишет произвольные файлы. Installed runner сам
читает только явно подготовленную consumer configuration в своей directory.
Никакие node/server configuration файлы не монтируются туда автоматически.
Доставка/допуск реальной consumer configuration и native exec относятся к
следующему pipeline task, не к данному socket API.

Inspect проверяет:

- actual Id, loaded transient unit, exact FragmentPath, numeric User/Group,
  Slice, InvocationID и ControlGroup;
- все hardening properties и отсутствие extra exec hooks/environment files;
  empty arrays запрашиваются структурированно через D-Bus, а не выводятся
  из отсутствующей строки `systemctl show`;
- structured ExecStart и ExecStartEx: единственный installed runner, точный
  argv и отсутствие exec-prefix flags, обходящих credentials/sandbox;
- nondelegated domain cgroup без дочерних cgroups; bounded cgroup.procs;
  для каждого текущего PID — actual UID/GID/groups, NoNewPrivs, capabilities,
  exact `/proc/<pid>/cgroup`, stable process start ticks;
- неизменность unit facts после kernel inspection и текущий kernel boot ID.

Исчезновение PID в ходе проверки — unknown, не empty proof. Отсутствующий unit,
удалённый cgroup, неверные свойства/UID/Exec/InvocationID не разрешают stop
соседнего unit. Retire не вызывает systemctl stop/reset-failed по имени: после
empty proof manager может оставить failed/active-exited OS unit или убрать
пустой cgroup. Повторное наблюдение отсутствующего cgroup остаётся unknown.
Сбор этих OS objects — отдельная installed administration policy, не бизнес-
журнал. Нет unbounded list-units, host `/proc` walk или prefix inventory.

## Bounds, cancellation and lost ACK

Default socket: до 8 peers, 2 s на request/response/ACK I/O. Default owner:
10 s ожидания результата и до 256 active/fenced scopes; configuration caps
16 peers / 4096 scopes / 30 s. Fences не вытесняются: при исчерпании capacity
следует backpressure, а не retry неизвестного эффекта.

Каждый systemd tool имеет deadline 3 s, общий stdout+stderr cap 64 KiB,
ограниченные stream buffers и retained kill/reap owner. Kernel files имеют
ограниченные reads с O_NONBLOCK/O_NOFOLLOW, FIFO/symlink refusal; максимум
128 PID и 256 directory entries на cgroup. Число manager calls фиксировано
схемой (10 на unit sample, два sample на inspect), не размером всего host.
Owner timeout не отменяет внешний syscall и не выдаётся за физическую
quiescence: action/cleanup остаются принадлежащими daemon до завершения своих
конечных OS calls. Shutdown, не дождавшийся cleanup, возвращает unknown.

Cancellation/timeout caller не отменяет единственную action task. После
завершения abandoned start supervisor пытается retire только проверенную
собственную reference. Потерянный OS launch ACK также вызывает addressed
inspection/cleanup, но verdict остаётся unknown, даже если cleanup удался.
Если после exec обнаружено несовпадение — unknown и fence; mismatch нельзя
убить «для уборки». Если response не получил peer receipt ACK — start cleanup
остаётся у supervisor. ACK не даёт exactly-once обещания; caller, потерявший
связь на любой границе, не должен повторять start.

Новый daemon не загружает fences/work/ready/allocation из файла, не запускает
старый runner и не называет отсутствие старой process-local task empty proof.
Reconciliation server epoch и полного native lifecycle подключается следующим
composition task. `UnboundHostScopes` до этого явно отказывает; никаких
`supports_*`/`scope_retirement_v1` flags, подкреплённых fake SessionApi, нет.

## Проверки и честная граница acceptance

`mop dev test host_scope` — finite parsing/peer/path/epoch/token/refusal,
current admission, active action/cancellation/cleanup, bounded capture,
manager property parsing. Fake adapters проверяют контракты, не containment.

`mop driver scope-probe` — единственный fixture frontend, без MCP declaration.
Он строит новую image из узкого source snapshot (без `.env`, login files,
checkout mounts), создаёт UUID/label-owned Docker container с private cgroup
namespace, default private PID namespace и `--network=none`, проверяет actual
HostConfig до start. Нет host cgroup bind, Docker socket mount, host PID/network,
изменений host systemd/installer или существующих containers. Docker resources
чистятся только по точному UUID name + собственному label.

Fixture PID 1 заменяет mount только внутри собственного private mount/cgroup
namespace, закрывает запись в `/sys`, `/proc/sys` и sysrq view, проверяет
создание writable private cgroup-v2 subtree, затем запускает свой systemd. Если kernel это запрещает, exit 77 и явный blocked reason — не green
acceptance. Иные ошибки настоящего запуска/проверок — failing exit, не skip.

В успешно выполненном gate настоящие разные node/body UID, root supervisor,
реальный fixed installed test runner, `setsid` + double fork. Kernel независимо
подтверждает detached descendant в exact cgroup, неправильная InvocationID
не убивает child, правильный retire читает empty subtree. Body negative checks:
node credential/socket denied, cgroup migration denied, system manager/system
bus/user runtime недоступны. Это actual OS containment acceptance, но HTTP
admission здесь fixture guard, native CLI не запускался. Real server admission,
consumer provisioning, native readiness/producer и installed rollout ещё не
приняты. Успешный private gate не включает production capability сам собой.
