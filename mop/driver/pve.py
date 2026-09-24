"""Proxmox: a body is an LXC container on a Proxmox node

Тело папета — контейнер LXC на узле-гипервизоре.

Цель — изоляция на папета: своё дерево, свой тулчейн, своя квота диска, и
сосед по узлу этого не видит. У драйвера host всё это общее, и единственной
границей между папетами был каталог.

Всё выводится из имени. Имя тела = имя папета = hostname контейнера; VMID и
адрес считаются из него чистыми функциями. VMID в модель mop не входит — это
деталь драйвера: второе имя для того же означало бы второе место, отвечающее
на вопрос «чей это папет», ровно то, от чего предостерегает правило о проекте.
Отсюда же берётся проверяемость: ни vmid_of, ни address_of не ходят никуда,
и обе проверены в tests/driver.py.

Две дороги в тело, и обе названы:
  основная  — ssh под ключом узла (не мастера: мастер и так ходит ssh на узлы,
              но вторая дорога в тело шла бы мимо единственного места, где
              проверяется проектирование — агента);
  аварийная — `mop-pve exec` через root-обёртку на гипервизоре, которая сама
              проверяет диапазон VMID и что hostname тела начинается с pu-.
              Нужна ровно тогда, когда у тела сломана сеть, sshd или права на
              authorized_keys.

Жизненный цикл — той же обёрткой. В REST API Proxmox нет ни exec внутрь LXC,
ни записи файла, так что вести цикл по API, а работу с телом — обёрткой
значило бы два механизма там, где хватает одного. Обёртка узкая и проверяет
каждый глагол; `pct` целиком пользователю пула не выдан — это был бы root на
гипервизоре вместе с соседними боевыми контейнерами.
"""
import asyncio
import hashlib
import ipaddress
import os
import re
import time
import shlex

from .. import config
from . import HOME, PREFIX, SERVER_PUB, bad_name, sh, project_of_name, valid_name, why

USER = config.get("MOP_USER")
# Тело — вещь сама по себе: у него свой $HOME, свои процессы и свой ssh.
IS_CONTAINER = True

# Обёртка на гипервизоре — единственная дорога к жизненному циклу тел.
WRAPPER = "/usr/local/sbin/mop-pve"
SUDO = ("sudo", "-n", WRAPPER)

STORAGE = config.get("MOP_PVE_STORAGE")
TEMPLATE = config.get("MOP_PVE_TEMPLATE")
BRIDGE = config.get("MOP_PVE_BRIDGE")
SUBNET = config.get("MOP_PVE_SUBNET")
# Ядра и диск тела драйвер не задаёт: тело наследует их от образа проекта, а
# в образ их вписывает сборка — из `.mop` самого проекта, подрезанного
# потолком узла.
#
# Память -- другое дело (#197): она свойство папета, и её потолок приезжает
# спекой (PU_MEM_MB), а ensure ставит его телу на каждом подъёме. Стоит он на
# теле, а не на задаче Nomad: `pct` исполняется демоном, а не потомком
# задачи, поэтому cgroup задачи не ограничивает ничего. Узел своё слово
# говорит размещением: meta mop_mem_cap_mb против ограничения в спеке.

# Диапазон VMID: первые 900 — тела, последние 100 — шаблоны проектов. Разводить
# их обязательно: снос папета глаголом destroy иначе унёс бы образ проекта, и
# заметили бы это на следующей сборке, а не сразу.
_BASE = config.num("MOP_PVE_VMID_BASE")


def _ranges(base):
    """(тела, шаблоны) гипервизора с этой базой: ((min, max), (min, max))."""
    return (base, base + 899), (base + 900, base + 999)


(BODY_MIN, BODY_MAX), (TMPL_MIN, TMPL_MAX) = _ranges(_BASE)

_NET = ipaddress.ip_network(SUBNET)
# Шлюз — первый адрес сети, он же адрес моста на самом гипервизоре: тела
# ходят наружу через NAT на узле. Настройка производная: то же значение нужно
# плейбуку, который поднимает мост, и вписанное там вторым местом однажды
# разошлось бы с этим.
GATEWAY = config.get("MOP_PVE_GATEWAY")
PREFIXLEN = _NET.prefixlen

# Ключ узла к своим телам и отдельный known_hosts. Отдельный не для порядка:
# пересозданное тело приезжает с новым ключом хоста, и общий known_hosts
# превратил бы каждый рецикл в отказ «ключ не совпал», который агент прочитает
# как молчащее тело.
SSH_KEY = f"{HOME}/.ssh/mop-body"
KNOWN_HOSTS = f"{HOME}/.ssh/known_hosts-mop-body"
SSH_CTRL = f"{HOME}/.ssh/mop-body-%C.sock"

# session.py исполняется внутри тела, и путь этот — путь в теле, а не на
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
        raise ValueError(bad_name(name))
    h = int(hashlib.sha1(name.encode()).hexdigest()[:8], 16)
    return BODY_MIN + h % (BODY_MAX - BODY_MIN + 1)


def template_name(project):
    """Имя шаблона проекта. Под охраной префикса pu- (обёртка на гипервизоре
    пускает только такие), но не имя папета: иначе ростер тел показал бы образ
    живым папетом."""
    return f"{PREFIX}tmpl-{project}"


def template_vmid(project, base=_BASE):
    lo, hi = _ranges(base)[1]
    h = int(hashlib.sha1(project.encode()).hexdigest()[:8], 16)
    return lo + h % (hi - lo + 1)


def stage_name(project):
    """Имя СБОРОЧНОГО тела проекта (#60): в нём играется плейбук, и только
    потом оно становится образом. Под тем же префиксом pu-tmpl-, чтобы
    ростер тел его не показывал папетом, а `mop sweep` — назвал, но не снёс."""
    return f"{template_name(project)}-build"


def parse_list(text):
    """Вывод глагола list -> [(vmid, hostname, status)]. Один разбор на
    ростер тел, ростер образов и выбор сборочного номера."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            out.append((int(parts[0]), parts[1],
                        parts[2] if len(parts) > 2 else ""))
    return out


def stage_vmid(project, listing, base=_BASE):
    """Номер сборочного тела по тому, что стоит на узле (parse_list).

    Не хеш, а свободный номер: сборочное тело живёт минуты, а хеш второго
    имени столкнулся бы с образом соседнего проекта с вероятностью один к ста
    — и молча. Стоящее тело с именем сборки этого проекта возвращается КАК
    ЕСТЬ: это оборванная сборка, и следующий прогон обязан продолжить в
    ней, а не заводить ещё одну. Иначе — старший свободный номер диапазона
    шаблонов, не совпадающий с номером образа: два номера одному проекту
    нужны одновременно, пока образ подменяется."""
    mine = template_vmid(project, base)
    lo, hi = _ranges(base)[1]
    taken = {}
    for vmid, name, _ in listing:
        taken[vmid] = name
        if name == stage_name(project):
            return vmid
    for vmid in range(hi, lo - 1, -1):
        if vmid != mine and vmid not in taken:
            return vmid
    raise RuntimeError(f"no free vmid for a build body of {project} in "
                       f"{lo}..{hi}: sweep old images (mop sweep)")


def address_of_vmid(vmid, subnet=SUBNET):
    """Адрес тела по его VMID: сеть плюс АБСОЛЮТНЫЙ номер. Одна формула на
    живые тела и на сборочные: диапазоны VMID не пересекаются (BODY_* против
    TMPL_*), значит не пересекаются и адреса — и это свойство держится само,
    а не проверкой.

    Абсолютный, а не смещение от базы этого узла (#58). База узловая
    (MOP_PVE_VMID_BASE в NODE_SCOPED), у второго гипервизора она своя, и
    смещение от неё сажало первое тело ЛЮБОГО гипервизора на сеть+2: два узла
    выдали бы один адрес двум телам, а маршрут «10.77.0.0/16 via кто?» из
    локалки стал бы неоднозначным — не при настройке, а позже и молча. От
    абсолютного номера разные базы дают непересекающиеся куски ОДНОЙ плоской
    сети, и маршрут к каждому гипервизору выходит однозначным сам собой
    (`routes`, deploy/pve.yml). Адрес при этом читается глазами: последние
    два октета — это VMID.

    Номер, не влезающий в сеть, — отказ, а не адрес соседней сети: уехавший
    за подсеть адрес не отказывает, он просто не отвечает."""
    net = _NET if subnet == SUBNET else ipaddress.ip_network(subnet)
    if not 2 <= vmid < net.num_addresses - 1:
        raise ValueError(f"vmid {vmid} does not fit the bodies' network {subnet}: "
                         f"lower MOP_PVE_VMID_BASE or widen MOP_PVE_SUBNET")
    return str(net.network_address + vmid)


def address_of(name):
    """Адрес тела. Тоже из имени — через VMID, одной цепочкой.

    Хранить адрес негде: `pct config` знал бы его, но спрашивать гипервизор на
    каждую пробу состояния значит платить за пробу процессом."""
    return address_of_vmid(vmid_of(name))


def address(name):
    """Где сервер найдёт тело (контракт, #151): адрес тела из имени."""
    return address_of(name)


def template_address(project):
    """Адрес тела проекта, ПОКА ОНО СОБИРАЕТСЯ.

    Считается из VMID шаблона тем же способом, что и у живого тела, и это
    не косметика. Раньше здесь стоял «шлюз плюс один», один и тот же для
    всех проектов, а рядом — довод, что пересечься адресам негде: живые тела
    якобы идут выше. Довод неверен дважды. Две сборки на одном гипервизоре
    всегда садились на один адрес (поймано 22.09: rugent и rudesktop
    одновременно, оба 10.77.0.2 — ssh уходил в чужой контейнер, и прогон не
    падал, а ВИС: apt спал в anon_pipe_write, ansible ждал в ep_poll, обе
    стороны живы, таймаута нет). И «выше» тоже не так: address_of начинает
    ровно с сети+2, то есть со шлюза плюс один, — тело с VMID в начале
    диапазона получило бы тот же адрес."""
    return address_of_vmid(template_vmid(project))


def cidr_of(name):
    return f"{address_of(name)}/{PREFIXLEN}"


def routes_of(subnet, base):
    """Маршруты к телам гипервизора с этой базой VMID: [CIDR], покрывающие
    ровно адреса его диапазона — тела и шаблоны, base..base+999 (#59).

    Тела стоят за NAT узла, и дорога к ним снаружи одна — через сам узел:
    FORWARD пропускает в обе стороны, MASQUERADE трогает только исходящие,
    и серверу не хватает ровно маршрута. Покрытие точное, а не «вся сеть
    через этот узел»: сеть одна и плоская, а гипервизоров может быть
    несколько, и маршруты двух узлов не должны налезать друг на друга.
    Отсюда summarize_address_range — минимальный набор выровненных блоков,
    без лишнего адреса с обеих сторон.

    Аргументы явные, а не настройки модуля: считается на управляющей
    машине за КАЖДЫЙ гипервизор с ЕГО базой и сетью (deploy/pve.yml), а
    модуль читает настройки той машины, где импортирован."""
    net = ipaddress.ip_network(subnet)
    first = net.network_address + base
    last = net.network_address + base + 999
    if last >= net.broadcast_address:
        raise ValueError(f"vmid base {base} does not fit the bodies' network "
                         f"{subnet}: lower MOP_PVE_VMID_BASE or widen MOP_PVE_SUBNET")
    return [str(n) for n in ipaddress.summarize_address_range(first, last)]


def routes():
    """То же для этого узла."""
    return routes_of(SUBNET, _BASE)


def facts(base, project=None, listing=""):
    """Всё, что плейбукам нужно знать о драйвере pve на гипервизоре с этой
    базой VMID. -> dict; `mop driver pve-facts` печатает его JSON'ом (#158).

    Раньше плейбуки добывали это тремя копиями `python3 -c` и досчитывали
    jinja: длину префикса, VMID_MAX, разбор списка тел поиском ' имя '. Второе
    вычисление однажды разошлось бы с первым молча -- как адреса сборок
    (template_address) и номера двух гипервизоров (#101).

    База -- явный аргумент, а не настройка модуля: считается на управляющей
    машине за КАЖДЫЙ гипервизор с ЕГО базой (строка хоста в инвентаре).
    С project -- образ и сборочное тело проекта; listing -- вывод глагола
    `list` обёртки на этом узле: по нему выбирается сборочный номер и видно,
    что уже стоит."""
    net = ipaddress.ip_network(SUBNET)
    out = {"base": base, "vmid_max": _ranges(base)[1][1],
           "network": str(net.network_address), "prefix": net.prefixlen,
           "gateway": GATEWAY, "routes": routes_of(SUBNET, base),
           "wrapper": WRAPPER, "ssh_key": SSH_KEY, "known_hosts": KNOWN_HOSTS}
    if project is None:
        return out
    standing = parse_list(listing or "")
    names = {name for _, name, _ in standing}
    tmpl, stage = template_vmid(project, base), stage_vmid(project, standing, base)
    bodies = re.compile(f"^{re.escape(PREFIX + project)}-[0-9]+$")
    out.update(
        image={"vmid": tmpl, "name": template_name(project),
               "address": address_of_vmid(tmpl), "present": template_name(project) in names},
        stage={"vmid": stage, "name": stage_name(project),
               "address": address_of_vmid(stage), "present": stage_name(project) in names},
        bodies=[name for _, name, _ in standing if bodies.match(name)])
    return out


# ─── доступ в тело ───────────────────────────────────────────────────────
# accept-new, а не ask: тело поднимается без человека, и вопрос про ключ
# хоста запарковал бы врапер навсегда. Пересозданное тело меняет ключ, поэтому
# ensure вычищает старую запись явно — одного accept-new для этого мало.
_SSH_OPTS = (
    "-i", SSH_KEY,
    "-o", "BatchMode=yes",
    # Только наш ключ и только он. Без этого ssh предъявляет каждый ключ из
    # агента, тело отказывает по разу на каждый, и на четвёртом отказе OpenSSH
    # включает штраф за источник — дальше роняются и соединения с верным
    # ключом. Выглядит это как молчащее тело при исправном ssh.
    "-o", "IdentitiesOnly=yes",
    "-o", "PreferredAuthentications=publickey",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
    "-o", "ConnectTimeout=5",
    "-o", "LogLevel=ERROR",
    # ControlPersist — не оптимизация: без него каждая проба состояния платит
    # рукопожатием ssh, а проб на один `mop list` уходит по три на папета.
    "-o", "ControlMaster=auto",
    "-o", f"ControlPath={SSH_CTRL}",
    "-o", "ControlPersist=60",
)


def argv(name):
    """Префикс команды: ssh в тело под ключом узла."""
    return ["ssh", *_SSH_OPTS, f"{USER}@{address_of(name)}"]


def run_argv(name):
    """Чем узел запускает в теле внутренний врапер.

    То же соединение, но без мультиплексирования — и это не оптимизация
    наоборот. Соединение врапера живёт столько же, сколько папет; повиснув
    клиентом на общем master'е, оно умирает вместе с ним, когда тот уходит по
    ControlPersist. Поймано на живом контейнерном папете: ssh отдал 255 сразу
    после рестарта, Nomad прочитал это как падение задачи и перезапустил
    здорового папета на ровном месте.

    Keepalive по той же причине: за NAT узла молчащее соединение однажды
    выпадет из таблицы, и врапер будет считать папета живым, разговаривая с
    дырой."""
    # Переопределения ПЕРЕД общим списком: у ssh побеждает первая
    # встреченная опция (ssh_config(5)), и `ControlMaster=no` после `auto`
    # не действовал — врапер молча мультиплексировался через мастер-сокет
    # агента и умирал с ним при каждом рестарте юнита (#72). Проверка
    # порядка в tests/driver.py.
    return ["ssh",
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            *_SSH_OPTS,
            f"{USER}@{address_of(name)}"]


def repair_argv(name):
    """Аварийный путь: `pct exec` через root-обёртку на гипервизоре.

    Нужен ровно тогда, когда основной не работает — у тела сломана сеть, sshd
    или права на authorized_keys, — поэтому он обязан не идти по ssh."""
    return [*SUDO, "exec", str(vmid_of(name))]


def attach_argv(name):
    """Чем человек входит в сессию. `mop attach` доводит его ssh до узла, а
    дальше это — второй ssh, уже внутрь тела: на гипервизоре tmux-сервера
    папета нет вовсе."""
    return ["ssh", "-t", *_SSH_OPTS, f"{USER}@{address_of(name)}",
            "tmux", "-L", name, "attach", "-t", name]


def projects_dir(name):
    """Транскрипты лежат внутри тела. Путь на гипервизоре дал бы молчаливый
    ноль расхода токенов у каждого контейнерного папета."""
    return f"{HOME}/.claude/projects"


# ─── жизненный цикл тела ─────────────────────────────────────────────────
def _pve_cmd(verb, *args):
    """Глагол обёртки на гипервизоре, строкой для шелла.

    Аргументы экранируются здесь и только здесь: обёртка исполняется под root,
    и имя, приехавшее с шины, обязано дойти до неё одним словом. Строка, а не
    вызов, потому что части команд нужен stdin (`push`)."""
    return " ".join(shlex.quote(str(x)) for x in (*SUDO, verb, *args))


async def _pve(verb, *args, timeout=600):
    """Тот же глагол, исполненный. -> (вывод, код)."""
    return await sh(_pve_cmd(verb, *args), timeout)


async def _forget_host_key(name):
    """Ключ хоста у пересозданного тела другой, и старая запись превратила бы
    каждое соединение в отказ «ключ не совпал» — агент прочитал бы это как
    молчащее тело. Зовётся и при создании, и при сносе."""
    await sh(f"ssh-keygen -R {address_of(name)} "
             f"-f {shlex.quote(KNOWN_HOSTS)} >/dev/null 2>&1 || true")


async def bodies():
    """Тела, стоящие на этом гипервизоре: [имя]. Ростер без Nomad.

    Каталог /tmp/tmux-<uid> на гипервизоре пуст — tmux-сервера папетов живут в
    телах, — поэтому перечисляет контейнеры сам гипервизор."""
    out, code = await _pve("list", timeout=60)
    if code not in (0, None):
        return []
    return sorted(n for _, n, _ in parse_list(out) if valid_name(n))


async def templates():
    """Сборочные тела и образы этого гипервизора: [{name, vmid, running}].

    Не тела папетов: `bodies()` их отсеивает, потому что имя шаблона не
    проходит valid_name, и это верно — папета в них нет. Но мусор из них
    выходит настоящий: запечатанный образ всегда СТОИТ, значит работающее
    тело с именем шаблона — это либо сборка прямо сейчас, либо сборка,
    которую оборвали. Различить их отсюда нечем, поэтому глагол только
    перечисляет; решает тот, кто знает, идёт ли сборка."""
    out, code = await _pve("list", timeout=60)
    if code not in (0, None):
        return []
    found = [{"name": n, "vmid": str(v), "running": st == "running"}
             for v, n, st in parse_list(out) if n.startswith(f"{PREFIX}tmpl-")]
    return sorted(found, key=lambda t: t["name"])


async def capacity():
    """Память гипервизора и место в хранилище тел.

    `df $HOME` здесь не значит ничего: тела лежат не в домашнем каталоге, а на
    томе хранилища, и подменить одно другим значит дать `mop gc` число, к делу
    не относящееся."""
    out, code = await _pve("capacity", STORAGE, timeout=60)
    if code not in (0, None) or not out.strip():
        return {"error": f"mop-pve capacity: {why(out, code)}"}
    try:
        mem_total, mem_free, disk_total, disk_free = out.split()[:4]
    except ValueError:
        return {"error": f"mop-pve capacity answered with garbage: {out.strip()!r}"}
    return {"path": f"{STORAGE} (pve)", "free_gb": int(disk_free),
            "total_gb": int(disk_total), "mem_total_mb": int(mem_total),
            "mem_free_mb": int(mem_free)}


async def _hostname(vmid):
    """Как зовут контейнер с этим номером; пусто, если его нет; None, если
    гипервизор не ответил за срок.

    Таймаут -- «не знаю», а не «нет» (#171): по этому ответу ensure решает,
    клонировать ли, и пустота на таймауте вела к клону поверх занятого vmid."""
    out, code = await _pve("list", timeout=60)
    if code is None:
        return None
    if code != 0:
        return ""
    for v, name, _ in parse_list(out):
        if v == vmid:
            return name
    return ""


# Отказ клона по блокировке (#195). Proxmox держит блокировку шаблона, пока
# с него снимается клон, и соседний клон того же шаблона получает отказ: на
# шаблоне -- `CT is locked (disk)`, на файле конфига -- `can't lock file
# ... got timeout`. После сборки образа Nomad поднимает тела проекта разом,
# и это штатная очередь, а не поломка: ждём здесь, не роняя задачу в круг
# рестарта.
CLONE_LOCKED = ("is locked", "can't lock file")
# Паузы между попытками, в сумме 119 с. Связанный клон держит шаблон секунды,
# полный -- до минуты-другой; двух минут хватает очереди из нескольких тел.
# Дольше не ждём: блокировка, пережившая две минуты, -- скорее всего,
# оставленная оборванным клоном, и её снимает человек, а не терпение.
CLONE_PAUSES = (2, 4, 8, 15, 15, 15, 15, 15, 15, 15)
_sleep = asyncio.sleep


def clone_locked(out):
    """Отказ клона -- чужая блокировка, которая уйдёт сама."""
    return any(s in out for s in CLONE_LOCKED)


async def _clone(src, vmid, name):
    """Клон шаблона, с повтором, пока шаблон занят чужим клоном. -> (вывод, код)."""
    for pause in (*CLONE_PAUSES, None):
        out, code = await _pve("clone", src, vmid, name, STORAGE, cidr_of(name),
                               GATEWAY, BRIDGE)
        if code == 0 or pause is None or not clone_locked(out):
            return out, code
        await _sleep(pause)


async def _clone_refusal(name, project, src, out, code):
    """Причина отказа клона. Совет собрать образ -- только когда образа
    действительно нет: иначе он шлёт оператора пересобирать исправный образ
    (#195)."""
    reason = why(out, code, 600)
    if code is not None and clone_locked(out):
        return (f"no body for {name}: template {src} stayed locked for "
                f"{sum(CLONE_PAUSES)}s: {reason}; if no clone or build is "
                f"running, the lock is stale: pct unlock {src} on the hypervisor")
    if await _hostname(src) == "":
        return (f"no body for {name}: {reason}; "
                f"build the project's image: mop driver build {project}")
    return f"no body for {name}: {reason}"


async def ensure(name, params=None):
    """Тело для папета: клон шаблона проекта, лимиты, адрес, старт.

    Идемпотентно: тело уже стоит — только поднимаем. Это и есть штатный путь,
    потому что `ensure` зовёт врапер на каждом подъёме папета, а рестарт
    аллокации случается куда чаще пересоздания.

    Креды проекта кладутся внутрь здесь: проект в этот момент известен, а папет
    без кредов шины читается мастером как живой, но молчащий — худший из
    отказов. `push` обёрткой, а не по ssh: на свежем теле ssh ещё не
    поднялся."""
    params = params or {}
    if not valid_name(name):
        return {"error": bad_name(name)}
    project = params.get("project") or project_of_name(name)
    vmid = vmid_of(name)
    mem = params.get("mem")
    if mem is not None and not (str(mem).isdigit() and int(mem) > 0):
        return {"error": f"{name}: PU_MEM_MB={mem!r} in the job spec is not a "
                         f"whole number of megabytes"}

    standing = await _hostname(vmid)
    if standing is None:
        return {"error": f"{name}: mop-pve list: {why('', None, 60)}; "
                         f"not cloning over a body that may stand"}
    if standing and standing != name:
        # Столкновение хешей либо чужой жилец в нашем диапазоне. Громко:
        # молча поднять не то тело значит отдать папету чужую работу.
        return {"error": f"vmid {vmid} is taken by {standing}, not {name} — "
                         f"rename or widen MOP_PVE_VMID_BASE"}
    created = False
    if not standing:
        src = template_vmid(project)
        out, code = await _clone(src, vmid, name)
        # Мутирующие шаги: таймаут -- отказ, не успех (#171).
        if code != 0:
            return {"error": await _clone_refusal(name, project, src, out, code)}
        created = True
        await _forget_host_key(name)
    else:
        # Адрес стоящего тела переутверждается на каждом подъёме, и это
        # переход, а не гигиена: формула адреса сменилась (#58), и тело,
        # стоящее со старым адресом, иначе читалось бы молчащим до рецикла —
        # вместе с работой в своём клоне. Обёртка сравнивает и пишет только
        # разницу, так что здоровому телу это не стоит ни переподключения
        # интерфейса, ни секунды.
        out, code = await _pve("net", vmid, cidr_of(name), GATEWAY, BRIDGE,
                               timeout=60)
        if code != 0:
            return {"error": f"{name}: body {vmid} won't take its address "
                             f"{address_of(name)}: {why(out, code, 60)}"}
        if out.strip():
            await _forget_host_key(name)

    if mem is not None:
        # Память -- свойство папета (#197): потолок из спеки, на каждом
        # подъёме и до start. Образ несёт память времён сборки, а стоящее
        # тело -- времён своего клона; ни то ни другое не знает о сегодняшнем
        # `.mop`. Ядра и диск остаются образу.
        out, code = await _pve("memory", vmid, mem, timeout=60)
        if code != 0:
            return {"error": f"{name}: body {vmid} won't take {mem} MB of "
                             f"memory: {why(out, code, 60)}"}

    out, code = await _pve("start", vmid, timeout=120)
    if code != 0:
        return {"error": f"{name}: body {vmid} won't start: "
                         f"{why(out, code, 120)}"}

    r = await _sync_package(name, vmid)
    if r.get("error"):
        return r
    r = await _seed(name, vmid)
    if r.get("error"):
        return r
    return {"name": name, "body": vmid, "created": created,
            "address": address_of(name)}


# Где на узле лежит пакет mop, который надо продублировать в тело: каталог
# проекта, от самого себя. Тот же, что привозит на узел `mop deploy`.
PACKAGE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def _sync_package(name, vmid):
    """Пакет mop внутрь тела — на каждом подъёме.

    Без этого правка session.py или usage.py доезжала бы до тела только со
    сборкой образа, то есть ловушка «правка логики тела доезжает прогоном
    mop deploy» (docs/DRIVER.md) была бы неправдой — молча. Поймано на
    `mop stat`: в теле лежал пакет времён сборки образа, у usage.py в нём ещё
    не было CLI, и расход контейнерного папета читался как ровный ноль —
    неотличимо от «папет ничего не потратил».

    Обёрткой, а не ssh: тот же путь, которым внутрь едет всё остальное, и
    работает он раньше, чем поднимется sshd."""
    blob = "/tmp/mop-package.tgz"
    tar = (f"tar czf - -C {shlex.quote(PACKAGE)} "
           f"--exclude=.git --exclude=__pycache__ --exclude=.env "
           f"--exclude=inventory.ini --exclude=inventory.yaml .")
    out, code = await sh(f"{tar} | {_pve_cmd('push', vmid, blob, '600')}", 300)
    if code != 0:
        return {"error": f"{name}: the mop package did not reach the body: "
                         f"{why(out, code, 300)}"}
    # Старую копию — в мусор, а не архив поверх неё: tar не удаляет то, чего
    # в пакете больше нет, и в теле копились бы файлы, снятые с узла
    # (поймано на переезде bin/ в mop/cli, #75: в теле остался весь старый
    # bin/). В этот момент из пакета в теле ничего не исполняется — врапер
    # идёт следом.
    out, code = await _pve(
        "exec", vmid,
        f"rm -rf {HOME}/mop && mkdir -p {HOME}/mop && tar xzf {blob} -C {HOME}/mop "
        f"&& rm -f {blob}",
        timeout=300)
    if code != 0:
        return {"error": f"{name}: the mop package did not unpack in the body: "
                         f"{why(out, code, 300)}"}
    return {}


def _seed_files():
    """Что узел переливает в тело на каждом подъёме: [(путь, режим)].

    Путь один и тот же с обеих сторон: у драйвера host папет живёт прямо в
    $HOME узла и все эти файлы у него уже есть, а тело обязано быть тем же,
    чем был узел. Новых прав это телу не даёт — ровно наоборот, именно этим
    список и оправдан.

    Список закрыт (настройка MOP_BODY_SEED) и собран из машины, а не из
    проекта мастера, — тот же довод, по которому закрыт WRITABLE у агента.

    Кред проекта здесь не едет (#114): его привозит в тело ответ bootstrap'а
    (`mop driver run`), а узел его больше не хранит."""
    out = []
    for rel in (p.strip() for p in config.get("MOP_BODY_SEED").split(",")):
        if rel:
            out.append((f"{HOME}/{rel}", "600"))
    return out


async def _seed(name, vmid):
    """Перелить в тело то, без чего папет поднимется и будет молчать."""
    for path, mode in _seed_files():
        if not os.path.exists(path):
            # Нет — не отказ: ключей LLM у профиля claude не бывает вовсе, а
            # ключ узла зовётся то id_rsa, то id_ed25519.
            continue
        out, code = await sh(
            f"{_pve_cmd('push', vmid, path, mode)} < {shlex.quote(path)}", 120)
        if code != 0:
            return {"error": f"{name}: {path} did not reach the body: "
                             f"{why(out, code, 120)}"}
    return {}


async def admit(name, let_in):
    """Впустить ключ сервера в тело на время bootstrap'а (#62) либо
    выпустить (let_in=False). Глагол keys заменяет файл целиком, и ключ узла
    в нём есть всегда — иначе, выпуская сервер, узел запер бы тело от себя.

    Только на время: постоянный ключ в образе был бы второй дорогой к телу
    мимо агента, то есть мимо единственной проверки проектирования, — ровно
    то, от чего отказались для ключа мастера при сборке образа.

    Нет ключа сервера на узле — исключение, а не отказ тела: это узел не
    докатан, и текст тот же, что давал bootstrap до #151."""
    pubkey = None
    if let_in:
        try:
            with open(SERVER_PUB) as f:
                pubkey = f.read().strip()
        except FileNotFoundError:
            raise RuntimeError(f"no server key on this node ({SERVER_PUB}) — "
                               f"run mop deploy")
    if not valid_name(name):
        return {"error": bad_name(name)}
    try:
        with open(f"{SSH_KEY}.pub") as f:
            text = f.read().strip() + "\n"
    except FileNotFoundError:
        return {"error": f"no node key {SSH_KEY}.pub — run mop deploy"}
    if pubkey:
        text += pubkey.strip() + "\n"
    out, code = await sh(f"printf %s {shlex.quote(text)} | "
                         f"{_pve_cmd('keys', vmid_of(name))}", 60)
    if code != 0:
        return {"error": f"{name}: keys: {why(out, code, 60)}"}
    return {"admitted": bool(pubkey)}


def tar_of(files, home):
    """[(абсолютный путь, bytes)] -> tar с путями от home, 0600. Чистая
    функция (#137). Путь вне дома -- отказ: распаковывает пользователь пула
    у себя дома, и куда ещё, кроме дома, ему класть нечего."""
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for path, data in files:
            rel = os.path.relpath(os.path.normpath(path), home)
            if not path.startswith(home.rstrip("/") + "/") or rel.startswith(".."):
                raise ValueError(f"{path}: outside {home}")
            info = tarfile.TarInfo(rel)
            info.size, info.mode, info.mtime = len(data), 0o600, int(time.time())
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def push_many(name, files):
    """Положить файлы в тело одним вызовом обёртки (#137): tar через
    `mop-pve unpack` -- один pct exec от пользователя пула, а не четыре pct на
    файл. Обёрткой, а не по ssh: этим же путём внутрь едет то, без чего ssh
    ещё не работает. Белый список путей проверяет звавший -- драйвер
    транспорт, а не право."""
    if not valid_name(name):
        return {"error": bad_name(name)}
    try:
        blob = tar_of(files, HOME)
    except ValueError as e:
        return {"error": str(e)}
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix="mop-push-")     # свой у каждого вызова
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        out, code = await sh(f"{_pve_cmd('unpack', vmid_of(name))} < {shlex.quote(tmp)}", 120)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if code != 0:
        return {"error": f"{name}: unpack: {why(out, code, 120)}"}
    return {"written": [p for p, _ in files]}


async def push(name, path, data):
    """Один файл в тело: 600, владелец -- пользователь пула. Частный случай
    push_many."""
    r = await push_many(name, [(path, data)])
    return r if r.get("error") else {"written": path}


async def destroy(name):
    """Снести тело целиком. Следующий `ensure` сделает новое из шаблона.

    У host на этом месте чистка клона — узел снести нельзя. Здесь можно, и
    это ровно то, чего от рецикла ждут: чистое дерево без следов прошлой
    работы, включая то, что `git clean` не выметает."""
    if not valid_name(name):
        return {"error": bad_name(name)}
    vmid = vmid_of(name)
    out, code = await _pve("destroy", vmid, timeout=600)
    if code != 0:
        return {"error": f"{name}: body {vmid} won't go: {why(out, code, 600)}"}
    await _forget_host_key(name)
    return {"destroyed": vmid, "target": f"body {vmid}"}
