"""Proxmox: a body is an LXC container on a hypervisor node

Тело папета — контейнер LXC на узле-гипервизоре.

Цель — изоляция НА ПАПЕТА: своё дерево, свой тулчейн, своя квота диска, и
сосед по узлу этого не видит. У драйвера host всё это общее, и единственной
границей между папетами был каталог.

ВСЁ ВЫВОДИТСЯ ИЗ ИМЕНИ. Имя тела = имя папета = hostname контейнера; VMID и
адрес считаются из него чистыми функциями. VMID в модель mop НЕ входит — это
деталь драйвера: второе имя для того же означало бы второе место, отвечающее
на вопрос «чей это папет», ровно то, от чего предостерегает правило о шарде.
Отсюда же берётся проверяемость: ни vmid_of, ни address_of не ходят никуда,
и обе проверены в tests/driver.py.

ДВЕ ДОРОГИ В ТЕЛО, и обе названы:
  основная  — ssh под ключом УЗЛА (не мастера: мастер и так ходит ssh на узлы,
              но вторая дорога в тело шла бы мимо единственного места, где
              проверяется шардирование — агента);
  аварийная — `mop-pve exec` через root-обёртку на гипервизоре, которая сама
              проверяет диапазон VMID и что hostname тела начинается с pu-.
              Нужна ровно тогда, когда у тела сломана сеть, sshd или права на
              authorized_keys.

ЖИЗНЕННЫЙ ЦИКЛ — ТОЙ ЖЕ ОБЁРТКОЙ. В REST API Proxmox нет ни exec внутрь LXC,
ни записи файла, так что вести цикл по API, а работу с телом — обёрткой
значило бы два механизма там, где хватает одного. Обёртка узкая и проверяет
каждый глагол; `pct` целиком пользователю пула не выдан — это был бы root на
гипервизоре вместе с соседними боевыми контейнерами.
"""
import hashlib
import ipaddress
import os
import shlex

from .. import config
from . import sh, valid_name

USER = config.get("MOP_USER")
HOME = config.get("MOP_HOME")
PREFIX = "pu-"

# Обёртка на гипервизоре — единственная дорога к жизненному циклу тел.
WRAPPER = "/usr/local/sbin/mop-pve"
SUDO = ("sudo", "-n", WRAPPER)

STORAGE = config.get("MOP_PVE_STORAGE")
TEMPLATE = config.get("MOP_PVE_TEMPLATE")
BRIDGE = config.get("MOP_PVE_BRIDGE")
SUBNET = config.get("MOP_PVE_SUBNET")
CORES = config.num("MOP_PVE_CORES")
DISK_GB = config.num("MOP_PVE_DISK_GB")
# Потолок памяти ставится ТЕЛУ, и настройка та же, что у задачи Nomad. Задача
# папета больше не содержит: `pct` исполняется демоном, а не потомком задачи,
# поэтому cgroup задачи не ограничивает ничего, а MemoryMB в спеке вырождается
# в бухгалтерию слотов. Не поставить потолок здесь — значит снять его вовсе.
MEM_MAX_MB = config.num("MOP_PUPPET_MEM_MAX_MB")

# Диапазон VMID: первые 900 — тела, последние 100 — шаблоны шардов. Разводить
# их обязательно: снос папета глаголом destroy иначе унёс бы образ шарда, и
# заметили бы это на следующей сборке, а не сразу.
_BASE = config.num("MOP_PVE_VMID_BASE")
BODY_MIN, BODY_MAX = _BASE, _BASE + 899
TMPL_MIN, TMPL_MAX = _BASE + 900, _BASE + 999

_NET = ipaddress.ip_network(SUBNET)
# Шлюз — первый адрес сети, он же адрес моста на самом гипервизоре: тела
# ходят наружу через NAT на узле. Настройка ПРОИЗВОДНАЯ: то же значение нужно
# плейбуку, который поднимает мост, и вписанное там вторым местом однажды
# разошлось бы с этим.
GATEWAY = config.get("MOP_PVE_GATEWAY")
PREFIXLEN = _NET.prefixlen

# Ключ УЗЛА к своим телам и отдельный known_hosts. Отдельный не для порядка:
# пересозданное тело приезжает с новым ключом хоста, и общий known_hosts
# превратил бы каждый рецикл в отказ «ключ не совпал», который агент прочитает
# как молчащее тело.
SSH_KEY = f"{HOME}/.ssh/mop-body"
KNOWN_HOSTS = f"{HOME}/.ssh/known_hosts-mop-body"
SSH_CTRL = f"{HOME}/.ssh/mop-body-%C.sock"

# session.py исполняется ВНУТРИ тела, и путь этот — путь в теле, а не на
# гипервизоре. Пакет mop туда привозит сборка шаблона.
SESSION_PY = f"{HOME}/mop/mop/session.py"


# ─── имя -> тело ─────────────────────────────────────────────────────────
def vmid_of(name):
    """VMID тела. Чистая функция имени, а не запись в реестре.

    Реестр («какой у pu-mop-1 номер») был бы вторым местом, отвечающим на
    вопрос о принадлежности папета, и разошёлся бы с первым ровно тогда, когда
    его потеряли. Хеш, а не счётчик, по той же причине: счётчик надо где-то
    хранить.

    Столкновение двух имён на одном номере возможно и поймано громко: ensure
    сверяет hostname занятого номера с ожидаемым."""
    if not valid_name(name):
        raise ValueError(f"name {name!r} doesn't look like {PREFIX}<project>-<n>")
    h = int(hashlib.sha1(name.encode()).hexdigest()[:8], 16)
    return BODY_MIN + h % (BODY_MAX - BODY_MIN + 1)


def template_name(shard):
    """Имя шаблона шарда. Под охраной префикса pu- (обёртка на гипервизоре
    пускает только такие), но НЕ имя папета: иначе ростер тел показал бы образ
    живым папетом."""
    return f"{PREFIX}tmpl-{shard}"


def template_vmid(shard):
    h = int(hashlib.sha1(shard.encode()).hexdigest()[:8], 16)
    return TMPL_MIN + h % (TMPL_MAX - TMPL_MIN + 1)


def address_of(name):
    """Адрес тела. Тоже из имени — через VMID, одной цепочкой.

    Хранить адрес негде: `pct config` знал бы его, но спрашивать гипервизор на
    каждую пробу состояния значит платить за пробу процессом."""
    return str(_NET.network_address + 2 + (vmid_of(name) - BODY_MIN))


def cidr_of(name):
    return f"{address_of(name)}/{PREFIXLEN}"


# ─── доступ в тело ───────────────────────────────────────────────────────
# accept-new, а не ask: тело поднимается без человека, и вопрос про ключ
# хоста запарковал бы врапер навсегда. Пересозданное тело меняет ключ, поэтому
# ensure вычищает старую запись явно — одного accept-new для этого мало.
_SSH_OPTS = (
    "-i", SSH_KEY,
    "-o", "BatchMode=yes",
    # Только НАШ ключ и только он. Без этого ssh предъявляет каждый ключ из
    # агента, тело отказывает по разу на каждый, и на четвёртом отказе OpenSSH
    # включает штраф за источник — дальше роняются и соединения с верным
    # ключом. Выглядит это как молчащее тело при исправном ssh.
    "-o", "IdentitiesOnly=yes",
    "-o", "PreferredAuthentications=publickey",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
    "-o", "ConnectTimeout=5",
    "-o", "LogLevel=ERROR",
    # ControlPersist — не оптимизация: без него КАЖДАЯ проба состояния платит
    # рукопожатием ssh, а проб на один `mop list` уходит по три на папета.
    "-o", "ControlMaster=auto",
    "-o", f"ControlPath={SSH_CTRL}",
    "-o", "ControlPersist=60",
)


def argv(name):
    """Префикс команды: ssh в тело под ключом узла."""
    return ["ssh", *_SSH_OPTS, f"{USER}@{address_of(name)}"]


def repair_argv(name):
    """Аварийный путь: `pct exec` через root-обёртку на гипервизоре.

    Нужен ровно тогда, когда основной не работает — у тела сломана сеть, sshd
    или права на authorized_keys, — поэтому он обязан НЕ идти по ssh."""
    return [*SUDO, "exec", str(vmid_of(name))]


def attach_argv(name):
    """Чем человек входит в сессию. `mop attach` доводит его ssh до узла, а
    дальше это — второй ssh, уже внутрь тела: на гипервизоре tmux-сервера
    папета нет вовсе."""
    return ["ssh", "-t", *_SSH_OPTS, f"{USER}@{address_of(name)}",
            "tmux", "-L", name, "attach", "-t", name]


def projects_dir(name):
    """Транскрипты лежат ВНУТРИ тела. Путь на гипервизоре дал бы молчаливый
    ноль расхода токенов у каждого контейнерного папета."""
    return f"{HOME}/.claude/projects"


# ─── жизненный цикл тела ─────────────────────────────────────────────────
async def _pve(verb, *args, timeout=600):
    """Глагол обёртки на гипервизоре. -> (вывод, код).

    Аргументы экранируются здесь и только здесь: обёртка исполняется под root,
    и имя, приехавшее с шины, обязано дойти до неё одним словом."""
    return await sh(" ".join(shlex.quote(str(x)) for x in (*SUDO, verb, *args)),
                    timeout)


def _pve_cmd(verb, *args):
    """Та же команда строкой — для случаев, когда ей нужен stdin."""
    return " ".join(shlex.quote(str(x)) for x in (*SUDO, verb, *args))


async def bodies():
    """Тела, стоящие на этом гипервизоре: [имя]. Ростер без Nomad.

    Каталог /tmp/tmux-<uid> на гипервизоре пуст — tmux-сервера папетов живут в
    телах, — поэтому перечисляет контейнеры сам гипервизор."""
    out, code = await _pve("list", timeout=60)
    if code not in (0, None):
        return []
    names = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].startswith(PREFIX):
            names.append(parts[1])
    return sorted(n for n in names if valid_name(n))


async def capacity():
    """Память гипервизора и место в ХРАНИЛИЩЕ ТЕЛ.

    `df $HOME` здесь не значит ничего: тела лежат не в домашнем каталоге, а на
    томе хранилища, и подменить одно другим значит дать `mop gc` число, к делу
    не относящееся."""
    out, code = await _pve("capacity", STORAGE, timeout=60)
    if code not in (0, None) or not out.strip():
        return {"error": f"mop-pve capacity: {out.strip() or f'exit {code}'}"}
    try:
        mem_total, mem_free, disk_total, disk_free = out.split()[:4]
    except ValueError:
        return {"error": f"mop-pve capacity answered with garbage: {out.strip()!r}"}
    return {"path": f"{STORAGE} (pve)", "free_gb": int(disk_free),
            "total_gb": int(disk_total), "mem_total_mb": int(mem_total),
            "mem_free_mb": int(mem_free)}


async def _hostname(vmid):
    """Как зовут контейнер с этим номером; пусто, если его нет."""
    out, code = await _pve("list", timeout=60)
    if code not in (0, None):
        return ""
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0] == str(vmid):
            return parts[1] if len(parts) > 1 else ""
    return ""


async def ensure(name, params=None):
    """Тело для папета: клон шаблона шарда, лимиты, адрес, старт.

    Идемпотентно: тело уже стоит — только поднимаем. Это и есть штатный путь,
    потому что `ensure` зовёт врапер на КАЖДОМ подъёме папета, а рестарт
    аллокации случается куда чаще пересоздания.

    Креды шарда кладутся внутрь ЗДЕСЬ: шард в этот момент известен, а папет
    без кредов шины читается мастером как живой, но молчащий — худший из
    отказов. `push` обёрткой, а не по ssh: на свежем теле ssh ещё не
    поднялся."""
    params = params or {}
    if not valid_name(name):
        return {"error": f"name {name!r} doesn't look like {PREFIX}<project>-<n>"}
    shard = params.get("shard") or name[len(PREFIX):].rsplit("-", 1)[0]
    vmid = vmid_of(name)

    standing = await _hostname(vmid)
    if standing and standing != name:
        # Столкновение хешей либо чужой жилец в нашем диапазоне. Громко:
        # молча поднять не то тело значит отдать папету чужую работу.
        return {"error": f"vmid {vmid} is taken by {standing}, not {name} — "
                         f"rename or widen MOP_PVE_VMID_BASE"}
    created = False
    if not standing:
        src = template_vmid(shard)
        out, code = await _pve("clone", src, vmid, name, STORAGE, cidr_of(name),
                               GATEWAY, MEM_MAX_MB, BRIDGE, CORES)
        if code not in (0, None):
            return {"error": f"no body for {name}: {out.strip() or f'exit {code}'}; "
                             f"build the shard's image: mop driver build {shard}"}
        created = True
        # Ключ хоста у пересозданного тела ДРУГОЙ, и старая запись превратила
        # бы каждое соединение в отказ «ключ не совпал» — агент прочитал бы
        # это как молчащее тело.
        await sh(f"ssh-keygen -R {address_of(name)} "
                 f"-f {shlex.quote(KNOWN_HOSTS)} >/dev/null 2>&1 || true")

    out, code = await _pve("start", vmid, timeout=120)
    if code not in (0, None):
        return {"error": f"{name}: body {vmid} won't start: "
                         f"{out.strip() or f'exit {code}'}"}

    r = await _seed(name, vmid, shard)
    if r.get("error"):
        return r
    return {"name": name, "body": vmid, "created": created,
            "address": address_of(name)}


def _seed_files(shard):
    """Что узел переливает в тело на каждом подъёме: [(путь, режим)].

    Путь один и тот же с обеих сторон: у драйвера host папет живёт прямо в
    $HOME узла и все эти файлы у него уже есть, а тело обязано быть тем же,
    чем был узел. Новых прав это телу не даёт — ровно наоборот, именно этим
    список и оправдан.

    Список ЗАКРЫТ (настройка MOP_BODY_SEED) и собран из МАШИНЫ, а не из
    проекта мастера, — тот же довод, по которому закрыт WRITABLE у агента.

    Креды ШАРДА идут отдельной строкой: их имя зависит от шарда, а шард
    известен только здесь. Без них папет поднимется и будет молчать — худший
    из отказов, потому что мастеру он читается как живой."""
    out = [(f"{HOME}/.config/mop/bus-{shard}.json", "600")]
    for rel in (p.strip() for p in config.get("MOP_BODY_SEED").split(",")):
        if rel:
            out.append((f"{HOME}/{rel}", "600"))
    return out


async def _seed(name, vmid, shard):
    """Перелить в тело то, без чего папет поднимется и будет молчать."""
    for path, mode in _seed_files(shard):
        if not os.path.exists(path):
            # Нет — не отказ: ключей LLM у профиля claude не бывает вовсе, а
            # ключ узла зовётся то id_rsa, то id_ed25519.
            continue
        out, code = await sh(
            f"{_pve_cmd('push', vmid, path, mode)} < {shlex.quote(path)}", 120)
        if code not in (0, None):
            return {"error": f"{name}: {path} did not reach the body: "
                             f"{out.strip() or f'exit {code}'}"}
    return {}


async def push(name, path, data):
    """Положить файл в тело: 600, владельцем — пользователь пула.

    Обёрткой, а не по ssh: этим же путём внутрь едет то, без чего ssh ещё не
    работает. Белый список путей проверяет ЗВАВШИЙ — драйвер транспорт, а не
    право."""
    if not valid_name(name):
        return {"error": f"name {name!r} doesn't look like {PREFIX}<project>-<n>"}
    tmp = f"/tmp/mop-push-{os.getpid()}"
    try:
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                  "wb") as f:
            f.write(data)
        out, code = await sh(
            f"{_pve_cmd('push', vmid_of(name), path, '600')} < {shlex.quote(tmp)}",
            120)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if code not in (0, None):
        return {"error": f"{path}: {out.strip() or f'exit {code}'}"}
    return {"written": path}


async def destroy(name):
    """Снести тело целиком. Следующий `ensure` сделает новое из шаблона.

    У host на этом месте чистка клона — узел снести нельзя. Здесь можно, и
    это ровно то, чего от рецикла ждут: чистое дерево без следов прошлой
    работы, включая то, что `git clean` не выметает."""
    if not valid_name(name):
        return {"error": f"name {name!r} doesn't look like {PREFIX}<project>-<n>"}
    vmid = vmid_of(name)
    out, code = await _pve("destroy", vmid, timeout=600)
    if code not in (0, None):
        return {"error": f"{name}: body {vmid} won't go: {out.strip() or f'exit {code}'}"}
    await sh(f"ssh-keygen -R {address_of(name)} "
             f"-f {shlex.quote(KNOWN_HOSTS)} >/dev/null 2>&1 || true")
    return {"destroyed": vmid, "target": f"body {vmid}"}
