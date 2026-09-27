"""Реестр драйверов узла: один файл в пакете — один драйвер.

Nomad решает, где стоит папет, шина — как с ним говорить. Драйвер отвечает на
третий вопрос: в чём он живёт. У драйвера `host` тело — сам узел, и вся
сегодняшняя архитектура становится его частным случаем, а не веткой, которую
надо поддерживать отдельно. План целиком — docs/DRIVER.md.

Драйвер выбирает узел, а не папет, и довод не вкусовой: Nomad выбирает узел
уже после регистрации спеки, а врапер лежит в спеке. Мастер в момент сборки
спеки не знает, на какой машине окажется папет, и заложить туда `pct exec` не
может. Источник правды — группа в инвентаре; `mop server deploy` кладёт значение и в
`Environment=` юнита агента, и в `meta` клиента Nomad из одной переменной.
Агент драйвер из запроса не берёт: иначе мастер проекта A прислал бы своё
значение и заставил агента исполнить команду не там.

Только STDLIB, `config` и `fsutil` (тоже stdlib). Этот пакет живёт на узле
рядом с агентом, а агенту Nomad не нужен вовсе — в этом половина смысла
переезда на шину. Импорт `python-nomad` отсюда потянул бы его на каждый узел.

Контракт (docs/DRIVER.md), две половины в одном файле — узел и его тела;
глаголы гипервизора требуются только от драйвера с отдельными телами (#276):

    ensure(name, params)   поднять тело: клон шаблона, лимиты, адрес, старт
    destroy(name)          снести тело
    bodies()               что есть на этом узле — ростер без Nomad
    capacity()             память и место хранилища тел
    argv(name)             префикс команды: [] у host, ssh у контейнера
    run_argv(name)         чем узел запускает врапер в теле: соединение живёт
                           столько же, сколько папет
    push(name, path, data) положить файл внутрь (mop login)
    projects_dir(name)     где транскрипты — mop stat, usage
    attach_argv(name)      чем входит человек
    repair_argv(name)      аварийный путь, когда основной молчит
    admit(name, let_in)    впустить ключ сервера в тело на время bootstrap'а
                           (#62), let_in=False — выпустить; у host пусто: ключ
                           там лежит постоянно
    address(name)          где сервер найдёт тело: у host — сам узел, у
                           контейнера — адрес тела (#151)
    templates()            сборочные тела и образы узла; только у драйвера с
                           отдельными телами, у host глагола нет (#151, #276)
    SESSION_PY             путь к session.py внутри тела
    IS_CONTAINER           тела — отдельные объекты, а не сам узел (False у host)

Потребитель ветвится не по флагу, а зовёт контракт (#151): флаг, прочитанный
вне драйвера, — это переключатель типа, и третий драйвер проходил проверку
контракта, чтобы упасть у потребителя.

Разводить узел и тела по двум реестрам значит получить решётку «узел × тело»
и два места, отвечающих на один вопрос.
"""
import asyncio
import importlib
import os
import re

from ..common import config, fsutil, paths, plugins

# Глаголы контракта. Списки закрыты и проверяются громко при загрузке: агент
# зовёт их из петли, и отсутствующий argv прочитается там как «узел молчит».
#
# Контракт разрезан по тому, кому вопрос задан (#276). Один толстый список
# требовал от host глагол, смысла для него не имеющий, и host держал пустую
# заглушку ради одного вызова в агенте.
#   BODY_VERBS        тело: поднять, снести, войти, положить файл -- все драйверы
#   NODE_VERBS        узел: что на нём стоит и сколько места -- все драйверы,
#                     у host узел и есть хранилище тел
#   HYPERVISOR_VERBS  сборочные тела и образы -- только драйвер с отдельными
#                     телами (IS_CONTAINER); потребитель берёт их через
#                     hypervisor_verb и без них отвечает как host
BODY_VERBS = ("ensure", "destroy", "argv", "run_argv", "push", "push_many",
              "projects_dir", "attach_argv", "repair_argv", "admit", "address")
NODE_VERBS = ("bodies", "capacity")
HYPERVISOR_VERBS = ("templates",)
VERBS = BODY_VERBS + NODE_VERBS + HYPERVISOR_VERBS
# Всё, что потребители берут у модуля драйвера напрямую: глаголы, которые есть
# у каждого, и два значения. Глаголы гипервизора сюда не входят -- их зовут
# только через hypervisor_verb. tests/driver.py выводит этот список из кода
# потребителей и сверяет.
CONSUMED = BODY_VERBS + NODE_VERBS + ("SESSION_PY", "IS_CONTAINER")

DEFAULT = config.SETTINGS["MOP_DRIVER"]

# Соглашение об имени папета живёт здесь, потому что здесь его читают обе
# стороны. Форма pu-<проект>-<n> строится у мастера (cluster.next_name), а
# разбирается на узле: агентом, драйверами, внешним врапером. Узлу puppets не
# импортировать — он тянет python-nomad, — и пока общего stdlib-дома не было,
# каждый читатель держал свою копию префикса и своего rsplit (#47).
#
# Префикс не настраивается: на нём стоят глобы сторожа диска (включая
# переходные ~/wk/wk-*) и имена tmux-серверов. Сделать его переменной, пока
# сторож знает оба префикса буквально, — значит развести половины одного
# соглашения.
PREFIX = "pu-"

# Имя папета склеивается в шелл — и у host, и у драйвера контейнеров, — а
# приезжает оно с шины. Проверка поэтому одна, здесь: два списка допустимого
# разъехались бы молча, и разошлись бы они как раз на той стороне, где команда
# идёт внутрь чужой машины.
_NAME = re.compile(rf"^{PREFIX}[A-Za-z0-9][A-Za-z0-9._-]*-\d+$")

_CACHE = None


def until_ok(attempt, tries, pause, sleep):
    """Повторять пробу, пока не пройдёт. -> (прошла, последняя причина, попыток).

    attempt() -> (ok, why). Пауза -- между попытками, не после последней:
    отказ обязан приходить сразу, как только кончились повторы. Поймано на
    свежем теле (#73): первый ssh врапера ушёл в Connection timed out, а
    повтор Nomad через 17 с вошёл сразу, -- и каждый такой промах стоил
    падения задачи и круга рестарта."""
    why = None
    for n in range(1, tries + 1):
        ok, why = attempt()
        if ok:
            return True, None, n
        if n < tries:
            sleep(pause)
    return False, why, tries


def valid_name(name):
    """Похоже ли это на имя папета. Всё, что не похоже, в шелл не попадает."""
    return bool(name) and bool(_NAME.match(name))


def bad_name(name):
    """Текст отказа по имени — один на все места, где имя проверяют."""
    return f"name {name!r} doesn't look like {PREFIX}<project>-<n>"


def project_of(origin):
    """Проект по origin репозитория.

    Basename без .git, и это единственное определение проекта в системе.
    Живёт здесь, а не в puppets, потому что агент на узле puppets
    импортировать не может, а своя копия правила однажды разошлась бы (#154).
    Путь берётся из parse_origin (#165): basename всей строки давал мусор на
    вырожденных формах -- scp без группы (`git@h:p.git` -> «git@h:p»,
    имя папета pu-git@h:p-1) и хвостовой слеш (пустой проект). На реальных
    формах ответ тот же, что у basename строки (tests/origin.py, SAME): из
    него строятся имена живых папетов. Не разбирается или путь пуст --
    прежний ответ.

    Соблазн взять хеш от полного origin есть — тогда два одноимённых репозитория
    на разных хостах не слились бы в один проект. Но имена папетов уже строятся отсюда же
    (`pu-<проект>-<n>`), и завести рядом второе, более точное понятие «проект»
    значит получить два места, по-разному отвечающих на вопрос «чей это папет».
    Цена честная и названа: одинаковые basename делят проект ровно так же, как
    уже делят имена. Понадобится развести — сюда добавляется суффикс от
    sha256(origin), и больше никуда."""
    parsed = parse_origin(origin)
    name = os.path.basename(parsed[4]) if parsed else ""
    return name or os.path.basename(origin).removesuffix(".git")


_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")


def parse_origin(url):
    """origin -> (схема, пользователь, хост, порт, путь) либо None.

    Один разборщик на всех (#154): раньше хост из origin'а доставали три
    разных правила (gitlab, known_hosts, «похоже на origin»). Формы -- те же,
    что у git:
      scheme://[user@]host[:port]/path   схема в нижнем регистре
      [user@]host:path                   scp-форма -- это ssh, порта не бывает
      всё прочее со / или :              локальный путь, схема file, хоста нет
    Путь -- без хвостового / и .git. None -- голое имя или пусто: origin'ом
    это не является. Пустые пользователь и порт -- None; хост и путь бывают
    пустыми у мусора вроде «:x», решать о нём -- потребителю."""
    s = (url or "").strip()
    if not s or not any(c in s for c in ":/"):
        return None
    m = _SCHEME.match(s)
    if m:
        scheme = m.group(1).lower()
        netloc, _, path = s[m.end():].partition("/")
        user, _, hostport = netloc.rpartition("@")
        host, _, port = hostport.partition(":")
        if scheme == "file":
            path = "/" + path
    elif ":" in s and "/" not in s.split(":", 1)[0]:
        # Слеш до двоеточия -- локальный путь: так решает сам git.
        scheme, port = "ssh", ""
        left, _, path = s.partition(":")
        user, _, host = left.rpartition("@")
    else:
        scheme, user, host, port, path = "file", "", "", "", s
    path = path.removesuffix("/").removesuffix(".git")
    return scheme, user or None, host, port or None, path


def project_of_name(name):
    """Проект по имени папета: pu-<проект>-<n>. Откат для случая, когда клона
    ещё нет, — origin спросить не у кого, а имя уже есть. Без префикса —
    пусто, а не кусок чужой строки."""
    if not name.startswith(PREFIX):
        return ""
    return name[len(PREFIX):].rsplit("-", 1)[0]


# Где папет живёт в теле. Один путь и у мастера (mcp, delete называют его
# человеку), и у узла (агент меряет и пробует), и у врапера в спеке: врапер
# своих путей не собирает, job_spec отдаёт ему эти в окружении задачи (#155).
HOME = config.get("MOP_HOME")
# Ключи LLM-профилей на узле: подмножество .env, которое раздаёт `mop login`.
SECRETS_FILE = paths.under(HOME, paths.SECRETS_ENV)


# Публичный ключ сервера на узле: его впускают в тело на время bootstrap'а
# (#62). Кладёт `mop server deploy` (роль bus).
SERVER_PUB = paths.under(HOME, "bootstrap.pub")


def clone_dir(name):
    return f"{HOME}/puppets/{name}"


# Подготовка клона в теле (#247): снять старую сессию, освободить каталог,
# перенацелить, клонировать (с зеркалом образа, если оно есть). Ждёт в
# окружении PU_NAME, PU_ORIGIN, PU_PROJECT, HOME и `d` -- путь клона.
#
# Одна константа на два места. Во врапере спеки она стоит как и прежде --
# правка сессии по-прежнему доезжает перерегистрацией, слепок tests/spec.py
# байт в байт. И её же `mop driver run` исполняет в теле ДО bootstrap:
# сервер играет манифест проекта в песочницу, где клон уже стоит, и
# `mop_clone` означает то, что написано в docs/BOOTSTRAP.md. Старая спека
# при этом не ломается: её врапер видит .git на месте и клонировать не идёт
# -- это его штатная ветка host-узла, где клон переживает папетов.
CLONE_SH = r"""# The old session dies first, before anything touches the directory. It used to
# die at the very bottom, just before the new session was opened -- some 140
# lines and one `npx playwright install` later -- so a retarget removed the
# clone out from under a live claude.
tmux -L "$PU_NAME" kill-session -t "$PU_NAME" 2>/dev/null || true

# ...and whatever outlived it holding the directory as its cwd is killed too.
# A puppet's own MCP server did exactly that (2026-08-31): orphaned by the
# kill above, it kept answering `agents` from memory while every `send` failed
# for the rest of the session, because its cwd pointed at an unlinked inode.
# The same thing was behind the older "fatal: cannot change to '<clone>'".
# The clone may already be gone (the disk sweep can remove it), and the kernel
# marks such a cwd " (deleted)" -- match on the name with that suffix stripped.
free_dir() {
    local dir="$1" cwd pid
    for p in /proc/[0-9]*; do
        pid=${p##*/}
        cwd=$(readlink "$p/cwd" 2>/dev/null) || continue
        cwd=${cwd% (deleted)}
        case "$cwd" in
            "$dir"|"$dir"/*) kill "$pid" 2>/dev/null || true ;;
        esac
    done
}
free_dir "$d"

if [ -d "$d/.git" ] && [ "$(git -C "$d" remote get-url origin)" != "$PU_ORIGIN" ]; then
    rm -rf "$d"
fi
if [ ! -d "$d/.git" ]; then
    mkdir -p "$(dirname "$d")"
    # Зеркало проекта, если тело принесло его с образом: объекты берутся
    # локально, а недостающее -- то, что появилось в origin после сборки
    # образа, -- git дотягивает по сети сам. Клон остаётся полноценным и
    # свежим, отставание зеркала лечится обычным fetch, а не пересборкой.
    #
    # --dissociate не ставим намеренно: он копирует объекты в клон и съедает
    # весь выигрыш. Цена названа: клон зависит от зеркала, и снос зеркала
    # оставит его с битыми alternates -- поэтому зеркало лежит в образе, то
    # есть в том же теле и ровно столько же, сколько сам клон.
    #
    # Нет зеркала -- клонируем как раньше. У драйвера host его не бывает
    # вовсе, и ветка обязана быть тихой: отказ здесь означал бы папета,
    # который не поднимается на обычном узле.
    mirror="$HOME/.cache/mop-mirror/$PU_PROJECT.git"
    if [ -d "$mirror" ]; then
        git clone -q --reference "$mirror" "$PU_ORIGIN" "$d"
    else
        git clone -q "$PU_ORIGIN" "$d"
    fi
fi
"""

def target_dir(name):
    return f"{HOME}/.cache/target-{name}"


def project_creds(project):
    """Кред шины проекта в теле: его кладёт `mop driver run` на подъёме."""
    return paths.under(HOME, f"bus-{project}.json")


def project_secrets_dir(project):
    """Секреты проекта в теле (#127): их кладёт bootstrap при каждом старте."""
    return paths.under(HOME, "project-secrets", project)


class Tmux:
    """Строки скрипта для tmux папета: и сервер (-L), и сессия (-t) зовутся
    его именем. Только строки -- исполняет вызывающий, и имя до шелла доходит
    лишь после valid_name.

    Одно место на соглашение `tmux -L <имя> ... -t <имя>` (#268): его знали
    агент, sweep узла, `mop driver run` и оба драйвера, каждый своей строкой.
    Врапер спеки пишет его сам -- он едет в тело текстом."""

    def __init__(self, name):
        self.name = name
        self.base = f"tmux -L {name}"

    def alive(self):
        return f"{self.base} has-session -t {self.name} 2>/dev/null"

    def kill(self):
        return f"{self.base} kill-session -t {self.name}"

    def attach_argv(self):
        """Чем человек входит в сессию -- argv, не строка: его исполняют
        без шелла (`mop attach`)."""
        return ["tmux", "-L", self.name, "attach", "-t", self.name]

    def buffer(self):
        """Весь буфер, с историей."""
        return f"{self.base} capture-pane -p -t {self.name} -S -"

    def visible(self):
        return f"{self.base} capture-pane -p -t {self.name}"

    def keys(self, keys):
        return f"{self.base} send-keys -t {self.name} {keys}"

    def press(self, key):
        """Голая клавиша и экран после неё."""
        return f"{self.keys(key)}; sleep 1; {self.visible()}"

    def type(self, command):
        """Очистить строку, напечатать команду, Enter, экран. Кавычку в
        команде отбивает вызывающий: команда идёт в шелл одной строкой."""
        quoted = f"'{command}'"
        keys = (f"{self.keys('C-u')}; sleep 0.3; "
                f"{self.keys(quoted)}; sleep 0.3; ") if command else ""
        return keys + f"{self.keys('Enter')}; sleep 2; {self.visible()}"



def why(out, code, timeout=None):
    """Причина отказа шелла одной строкой: вывод, а если он пуст — код.

    Код None -- таймаут sh(), и он назван словами, со сроком, если звавший
    его знает (#171): мутирующий глагол, прочитавший «не успел» как «сделал»,
    отвечал успехом по наполовину снесённому каталогу."""
    if out.strip():
        return out.strip()
    if code is None:
        return "timed out" + (f" after {timeout}s" if timeout else "")
    return f"exit {code}"


def write_private(path, data):
    """Файл 600, атомарно: во временный рядом и rename. Так узел кладёт креды
    (агент, глагол write) и так драйвер host кладёт файл в своё тело — это
    одна и та же запись, и была скопирована дословно. Сама запись -- общая
    (fsutil, #153)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fsutil.write_private(path, data)


def contract(name, mod):
    """Модуль-плагин -> {session_py, is_container, doc}; RuntimeError при нарушении.

    Отдельная от загрузки функция, потому что проверяема без пула
    (tests/driver.py): ошибка контракта обязана находиться до живых папетов."""
    where = f"mop/driver/{name}.py"
    # Одно тело на узел или много — это разные вопросы к одному драйверу, и
    # спрашивать «пустой ли argv» вместо ответа значит выводить свойство из
    # побочного признака. Раздача файлов (`mop login`) на этом стоит: у host
    # запись в каждое тело была бы записью в тот же файл по разу на папета, а
    # отчёт обещал бы запись в тела, которых нет.
    if not isinstance(getattr(mod, "IS_CONTAINER", None), bool):
        raise RuntimeError(f"{where}: IS_CONTAINER — False when the body is the "
                           f"node itself, True when it is a thing of its own")
    # Какие глаголы требовать, решает тип тел: сборочные тела бывают только
    # там, где тела отдельны от узла. Поэтому IS_CONTAINER проверен раньше.
    demanded = BODY_VERBS + NODE_VERBS + (HYPERVISOR_VERBS if mod.IS_CONTAINER else ())
    for verb in demanded:
        fn = getattr(mod, verb, None)
        if not callable(fn):
            raise RuntimeError(f"{where}: no {verb}() — the contract is "
                               f"{', '.join(demanded)} (docs/DRIVER.md)")
    session_py = getattr(mod, "SESSION_PY", None)
    # Абсолютный, потому что исполняется внутри тела и из чужого каталога:
    # относительный там молча соберётся в `python3 session.py`, которого нет,
    # и живая сессия прочитается как мёртвая.
    if not isinstance(session_py, str) or not os.path.isabs(session_py):
        raise RuntimeError(f"{where}: SESSION_PY — absolute path to session.py "
                           f"inside the body")
    doc = (mod.__doc__ or "").strip().splitlines()
    return {"session_py": session_py,
            "is_container": mod.IS_CONTAINER,
            "doc": doc[0].strip() if doc else ""}


def drivers():
    """Весь реестр: {имя драйвера: контракт}. Имя файла = имя драйвера."""
    global _CACHE
    if _CACHE is None:
        _CACHE = plugins.discover(__file__, __package__, contract)
    return _CACHE


def get(name):
    """Контракт драйвера по имени либо None."""
    return drivers().get(name)


def require(name):
    """Контракт по имени; громкий отказ с перечнем, если такого нет."""
    d = get(name)
    if d is None:
        raise RuntimeError(f"no node driver {name or '(empty)'}; available: "
                           f"{', '.join(drivers())} (docs/DRIVER.md)")
    return d


def of_node(meta, node="this node"):
    """Имя драйвера узла по его meta в Nomad -- единственное правило (#175).

    Нет ключа или пусто -- драйвер по умолчанию: так ведёт себя узел,
    настроенный до того, как поле появилось. Пока пустое значение оставалось
    пустым здесь и превращалось в host в четырёх других местах, один и тот
    же узел был контейнерным для сборщика и host для ростера.

    Неизвестное имя -- опечатка в инвентаре -- громкий отказ с узлом и
    значением, а не догадка: сборщик считал его контейнером, mop delete
    падал на нём же."""
    name = (meta or {}).get("mop_driver") or DEFAULT
    if get(name) is None:
        raise RuntimeError(f"{node}: unknown driver '{name}'; available: "
                           f"{', '.join(drivers())} (docs/DRIVER.md)")
    return name


def is_container(name):
    """Тела на узле с этим драйвером — отдельные объекты (контейнеры), а не
    сам узел. Имя -- из of_node: неизвестное туда не доходит."""
    return require(name)["is_container"]


def separate_bodies(mod):
    """Отдельны ли тела от узла (#312): у host тело -- сам узел, и копия
    узла и есть файл каждого его папета; у контейнерного драйвера копия
    узла -- только то, что сеется в новое тело."""
    return bool(mod.IS_CONTAINER)


def hypervisor_verb(mod, verb):
    """Глагол гипервизора у модуля драйвера -> функция либо None (#276).

    Контракт требует его только от драйвера с отдельными телами; у host его
    нет, и потребитель на None отвечает так, как отвечал host: сборочных тел
    нет. Имя проверяется по списку, чтобы сюда не просочился глагол, который
    обязан быть у всех, -- его зовут напрямую."""
    if verb not in HYPERVISOR_VERBS:
        raise ValueError(f"{verb} is not a hypervisor verb; those are "
                         f"{', '.join(HYPERVISOR_VERBS)}")
    fn = getattr(mod, verb, None)
    return fn if callable(fn) else None


def module(name):
    """Сам модуль драйвера — после того как реестр проверил его контракт."""
    require(name)
    return importlib.import_module(f".{name}", __package__)


def current_name():
    """Драйвер этого узла — обычная узловая настройка (config.NODE_SCOPED).

    Спрашивают её двое с разным окружением: агент из юнита systemd и внешний
    врапер из процесса задачи Nomad. Пока значение жило строкой Environment= в
    юните, врапер его не видел вовсе и молча поднимал папета драйвером host —
    на гипервизоре это значит «прямо на гипервизоре, мимо тела».

    Из запроса не берётся никогда: иначе мастер проекта A прислал бы своё
    значение и заставил узел исполнить команду не там."""
    return of_node({"mop_driver": config.get("MOP_DRIVER")}, "this node (MOP_DRIVER)")


def current():
    """Модуль драйвера этого узла."""
    return module(current_name())


# ─── исполнение ──────────────────────────────────────────────────────────
async def sh(script, timeout=20, prefix=()):
    """Шелл: на самом узле (пустой префикс) либо внутри тела. -> (вывод, код).

    Скрипт всегда строка, а не список: префикс тела — это ssh, а ssh склеивает
    свои аргументы пробелом и отдаёт удалённому шеллу одной строкой. Список
    там молча пересобрался бы не в ту команду, поэтому форма одна и на узле, и
    в теле — строка, а кавычки расставляет вызывающий.

    Возврат (вывод, None) на таймауте отличается от (вывод, код): «не успел» и
    «ответил ненулевым» ведут в разные стороны, и молчащее тело нельзя
    прочитать как отказ команды."""
    if prefix:
        proc = await asyncio.create_subprocess_exec(
            *prefix, script,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    else:
        proc = await asyncio.create_subprocess_shell(
            script, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return "", None
    return out.decode(errors="replace"), proc.returncode
