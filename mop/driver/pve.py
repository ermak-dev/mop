"""Proxmox: a body is an LXC container on a Proxmox node

Тело папета — контейнер LXC на узле-гипервизоре.

Цель — изоляция на папета: своё дерево, свой тулчейн, своя квота диска, и
сосед по узлу этого не видит. У драйвера host всё это общее, и единственной
границей между папетами был каталог.

Всё выводится из имени. Имя тела = имя папета = hostname контейнера; VMID и
адрес считаются из него чистыми функциями. VMID в модель mop не входит — это
деталь драйвера: второе имя для того же означало бы второе место, отвечающее
на вопрос «чей это папет», ровно то, от чего предостерегает правило о шарде.
Отсюда же берётся проверяемость: ни vmid_of, ни address_of не ходят никуда,
и обе проверены в tests/driver.py.

Две дороги в тело, и обе названы:
  основная  — ssh под ключом узла (не мастера: мастер и так ходит ssh на узлы,
              но вторая дорога в тело шла бы мимо единственного места, где
              проверяется шардирование — агента);
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
import hashlib
import ipaddress
import os
import shlex

from .. import config
from . import HOME, PREFIX, bad_name, sh, shard_of_name, valid_name, why

USER = config.get("MOP_USER")
# Тело — вещь сама по себе: у него свой $HOME, свои процессы и свой ssh.
BODY_IS_NODE = False

# Обёртка на гипервизоре — единственная дорога к жизненному циклу тел.
WRAPPER = "/usr/local/sbin/mop-pve"
SUDO = ("sudo", "-n", WRAPPER)

STORAGE = config.get("MOP_PVE_STORAGE")
TEMPLATE = config.get("MOP_PVE_TEMPLATE")
BRIDGE = config.get("MOP_PVE_BRIDGE")
SUBNET = config.get("MOP_PVE_SUBNET")
# Память, ядра и диск тела драйвер не задаёт: тело наследует их от образа
# шарда, а в образ их вписывает сборка — из `.mop` самого проекта, подрезанного
# потолком узла. Носителем шардовых размеров становится образ, и на узел не
# едет ни одного числа. Поставь их здесь — и значения установки затёрли бы
# просьбу проекта.
#
# Потолок памяти при этом всё равно стоит на теле, а не на задаче Nomad:
# `pct` исполняется демоном, а не потомком задачи, поэтому cgroup задачи не
# ограничивает ничего, а MemoryMB в спеке вырождается в бухгалтерию слотов.

# Диапазон VMID: первые 900 — тела, последние 100 — шаблоны шардов. Разводить
# их обязательно: снос папета глаголом destroy иначе унёс бы образ шарда, и
# заметили бы это на следующей сборке, а не сразу.
_BASE = config.num("MOP_PVE_VMID_BASE")
BODY_MIN, BODY_MAX = _BASE, _BASE + 899
TMPL_MIN, TMPL_MAX = _BASE + 900, _BASE + 999

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


def template_name(shard):
    """Имя шаблона шарда. Под охраной префикса pu- (обёртка на гипервизоре
    пускает только такие), но не имя папета: иначе ростер тел показал бы образ
    живым папетом."""
    return f"{PREFIX}tmpl-{shard}"


def template_vmid(shard):
    h = int(hashlib.sha1(shard.encode()).hexdigest()[:8], 16)
    return TMPL_MIN + h % (TMPL_MAX - TMPL_MIN + 1)


def stage_name(shard):
    """Имя СБОРОЧНОГО тела шарда (#60): в нём играется плейбук, и только
    потом оно становится образом. Под тем же префиксом pu-tmpl-, чтобы
    ростер тел его не показывал папетом, а `mop sweep` — назвал, но не снёс."""
    return f"{template_name(shard)}-build"


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


def stage_vmid(shard, listing):
    """Номер сборочного тела по тому, что стоит на узле (parse_list).

    Не хеш, а свободный номер: сборочное тело живёт минуты, а хеш второго
    имени столкнулся бы с образом соседнего шарда с вероятностью один к ста
    — и молча. Стоящее тело с именем сборки этого шарда возвращается КАК
    ЕСТЬ: это оборванная сборка, и следующий прогон обязан продолжить в
    ней, а не заводить ещё одну. Иначе — старший свободный номер диапазона
    шаблонов, не совпадающий с номером образа: два номера одному шарду
    нужны одновременно, пока образ подменяется."""
    mine = template_vmid(shard)
    taken = {}
    for vmid, name, _ in listing:
        taken[vmid] = name
        if name == stage_name(shard):
            return vmid
    for vmid in range(TMPL_MAX, TMPL_MIN - 1, -1):
        if vmid != mine and vmid not in taken:
            return vmid
    raise RuntimeError(f"no free vmid for a build body of {shard} in "
                       f"{TMPL_MIN}..{TMPL_MAX}: sweep old images (mop sweep)")


def address_of_vmid(vmid):
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
    if not 2 <= vmid < _NET.num_addresses - 1:
        raise ValueError(f"vmid {vmid} does not fit the bodies' network {SUBNET}: "
                         f"lower MOP_PVE_VMID_BASE or widen MOP_PVE_SUBNET")
    return str(_NET.network_address + vmid)


def address_of(name):
    """Адрес тела. Тоже из имени — через VMID, одной цепочкой.

    Хранить адрес негде: `pct config` знал бы его, но спрашивать гипервизор на
    каждую пробу состояния значит платить за пробу процессом."""
    return address_of_vmid(vmid_of(name))


def template_address(shard):
    """Адрес тела шарда, ПОКА ОНО СОБИРАЕТСЯ.

    Считается из VMID шаблона тем же способом, что и у живого тела, и это
    не косметика. Раньше здесь стоял «шлюз плюс один», один и тот же для
    всех шардов, а рядом — довод, что пересечься адресам негде: живые тела
    якобы идут выше. Довод неверен дважды. Две сборки на одном гипервизоре
    всегда садились на один адрес (поймано 22.09: rugent и rudesktop
    одновременно, оба 10.77.0.2 — ssh уходил в чужой контейнер, и прогон не
    падал, а ВИС: apt спал в anon_pipe_write, ansible ждал в ep_poll, обе
    стороны живы, таймаута нет). И «выше» тоже не так: address_of начинает
    ровно с сети+2, то есть со шлюза плюс один, — тело с VMID в начале
    диапазона получило бы тот же адрес."""
    return address_of_vmid(template_vmid(shard))


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
    """Как зовут контейнер с этим номером; пусто, если его нет."""
    out, code = await _pve("list", timeout=60)
    if code not in (0, None):
        return ""
    for v, name, _ in parse_list(out):
        if v == vmid:
            return name
    return ""


async def ensure(name, params=None):
    """Тело для папета: клон шаблона шарда, лимиты, адрес, старт.

    Идемпотентно: тело уже стоит — только поднимаем. Это и есть штатный путь,
    потому что `ensure` зовёт врапер на каждом подъёме папета, а рестарт
    аллокации случается куда чаще пересоздания.

    Креды шарда кладутся внутрь здесь: шард в этот момент известен, а папет
    без кредов шины читается мастером как живой, но молчащий — худший из
    отказов. `push` обёрткой, а не по ssh: на свежем теле ssh ещё не
    поднялся."""
    params = params or {}
    if not valid_name(name):
        return {"error": bad_name(name)}
    shard = params.get("shard") or shard_of_name(name)
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
                               GATEWAY, BRIDGE)
        if code not in (0, None):
            return {"error": f"no body for {name}: {why(out, code)}; "
                             f"build the shard's image: mop driver build {shard}"}
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
        if code not in (0, None):
            return {"error": f"{name}: body {vmid} won't take its address "
                             f"{address_of(name)}: {why(out, code)}"}
        if out.strip():
            await _forget_host_key(name)

    out, code = await _pve("start", vmid, timeout=120)
    if code not in (0, None):
        return {"error": f"{name}: body {vmid} won't start: "
                         f"{why(out, code)}"}

    r = await _sync_package(name, vmid)
    if r.get("error"):
        return r
    r = await _seed(name, vmid, shard)
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
    if code not in (0, None):
        return {"error": f"{name}: the mop package did not reach the body: "
                         f"{why(out, code)}"}
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
    if code not in (0, None):
        return {"error": f"{name}: the mop package did not unpack in the body: "
                         f"{why(out, code)}"}
    return {}


def _seed_files(shard):
    """Что узел переливает в тело на каждом подъёме: [(путь, режим)].

    Путь один и тот же с обеих сторон: у драйвера host папет живёт прямо в
    $HOME узла и все эти файлы у него уже есть, а тело обязано быть тем же,
    чем был узел. Новых прав это телу не даёт — ровно наоборот, именно этим
    список и оправдан.

    Список закрыт (настройка MOP_BODY_SEED) и собран из машины, а не из
    проекта мастера, — тот же довод, по которому закрыт WRITABLE у агента.

    Креды шарда идут отдельной строкой: их имя зависит от шарда, а шард
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
                             f"{why(out, code)}"}
    return {}


async def admit(name, pubkey):
    """Впустить ключ сервера в тело на время bootstrap'а (#62) либо
    выпустить (pubkey=None). Глагол keys заменяет файл целиком, и ключ узла
    в нём есть всегда — иначе, выпуская сервер, узел запер бы тело от себя.

    Только на время: постоянный ключ в образе был бы второй дорогой к телу
    мимо агента, то есть мимо единственной проверки шардирования, — ровно
    то, от чего отказались для ключа мастера при сборке образа."""
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
    if code not in (0, None):
        return {"error": f"{name}: keys: {why(out, code)}"}
    return {"admitted": bool(pubkey)}


async def push(name, path, data):
    """Положить файл в тело: 600, владельцем — пользователь пула.

    Обёрткой, а не по ssh: этим же путём внутрь едет то, без чего ssh ещё не
    работает. Белый список путей проверяет звавший — драйвер транспорт, а не
    право."""
    if not valid_name(name):
        return {"error": bad_name(name)}
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
        return {"error": f"{path}: {why(out, code)}"}
    return {"written": path}


async def destroy(name):
    """Снести тело целиком. Следующий `ensure` сделает новое из шаблона.

    У host на этом месте чистка клона — узел снести нельзя. Здесь можно, и
    это ровно то, чего от рецикла ждут: чистое дерево без следов прошлой
    работы, включая то, что `git clean` не выметает."""
    if not valid_name(name):
        return {"error": bad_name(name)}
    vmid = vmid_of(name)
    out, code = await _pve("destroy", vmid, timeout=600)
    if code not in (0, None):
        return {"error": f"{name}: body {vmid} won't go: {why(out, code)}"}
    await _forget_host_key(name)
    return {"destroyed": vmid, "target": f"body {vmid}"}
