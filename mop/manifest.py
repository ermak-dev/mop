"""Манифест проекта: .mop/ в корне проекта, из его origin или из рабочей копии.

Здесь и чистый разбор (play, parts), и дорога за манифестом: зеркало или
каталог, файлы для ansible. Разбор жил в config, но нужен только здесь и
bootstrap'у, а config -- нижний слой (#156). Молчит и
печатает вызывающий: библиотека возвращает данные.

Два файла, и имена говорят, когда каждый играется (#61):

    .mop/sandbox.yaml    размеры тела (vars по конвенции PROJECT_SCOPED),
                         конфигурация проекта (остальные vars) и системные
                         пакеты (tasks) — ПЕЧЁТСЯ в образ проекта; на узле,
                         где тело равно узлу, играется прогоном deploy
    .mop/bootstrap.yaml  env-файлы и настройка окружения — играется при
                         КАЖДОМ старте песочницы (#62)

Раньше файлы звались node.yaml и workspace.yaml и обещали обратное тому,
что делал механизм: workspace пёкся в образ, node играл на машине-узле (на
гипервизоре — на самом Proxmox, где папета нет), а при старте не играло
ничто. Старые имена читаются переходно — оба как sandbox, ровно так, как они
и работали, — и возвращаются в `legacy`, чтобы вызывающий сказал об этом
вслух: непереехавший проект не должен молча остаться без манифеста. Порядок
переезда обязателен: mop читает оба имени → переезжают проекты → старое имя
снимается.

Тяжёлое — в sandbox: то, что лежит в bootstrap, платится на каждом подъёме
папета. Файлы, а не значения в аргументах: задачи и конфигурация манифеста —
структура, и пусть её кладёт yaml, а не json в командной строке.
"""
import os
import subprocess

from . import config, puppets

SANDBOX = ".mop/sandbox.yaml"
BOOTSTRAP = ".mop/bootstrap.yaml"


# ─── чистый разбор ───────────────────────────────────────────────────────
def parts(mvars):
    """vars манифеста -> (просьбы, конфигурация проекта, чужие имена).

    Три судьбы у одного словаря: ключи PROJECT_SCOPED — просьба о размерах
    (едет в образ числами); ключи, не являющиеся настройками mop, —
    конфигурация самого проекта (едет контекстом его задачам); ключи,
    совпадающие с настоящими настройками, — чужие: в контексте задач они
    затёрли бы правду машины (MOP_USER, MOP_HOME), поэтому отбрасываются
    громко, со списком.
    """
    asks = {k: str(v) for k, v in mvars.items() if k in config.PROJECT_SCOPED}
    mine = {k: v for k, v in mvars.items() if k not in config.SETTINGS}
    alien = sorted(k for k in mvars if k in config.SETTINGS and k not in config.PROJECT_SCOPED)
    return asks, mine, alien


def play(text):
    """`mop.yaml` -> ({vars}, [tasks]).

    Манифест проекта (#25): одна игра, где vars — и просьба (ключи
    PROJECT_SCOPED), и конфигурация его окружения, а tasks — само окружение
    сверх общего. Форма оговорена жёстко, и отход от неё — ValueError, а не
    «прочиталось как получилось»: молча потерянные tasks означают образ без
    окружения проекта, а молча потерянные vars — образ не тех размеров.

    yaml импортируется лениво и только здесь: пакет ездит на узлы, где
    pyyaml может не оказаться, а манифест читается исключительно на
    управляющей машине.
    """
    import yaml
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"mop.yaml is not YAML: {str(e).splitlines()[0]}")
    if not isinstance(doc, list) or len(doc) != 1 or not isinstance(doc[0], dict):
        raise ValueError("mop.yaml must be one play: a list of exactly one mapping")
    play = doc[0]
    # Отсутствие секции — нормально (нечего просить/ставить), но присутствие
    # не той формы — ошибка: `tasks: {}` не «пустые задачи», а опечатка,
    # из-за которой образ тихо остался бы без окружения проекта.
    of_vars = play.get("vars")
    tasks = play.get("tasks")
    if of_vars is None:
        of_vars = {}
    if tasks is None:
        tasks = []
    if not isinstance(of_vars, dict):
        raise ValueError("mop.yaml vars must be a mapping")
    if not isinstance(tasks, list):
        raise ValueError("mop.yaml tasks must be a list")
    return of_vars, tasks
# Переходные имена: читаются как sandbox, пока проекты не переехали.
LEGACY = (".mop/node.yaml", ".mop/workspace.yaml")


def clone_env(environ):
    """Окружение клона манифеста. Чистая функция (#141).

    Новый ключ хоста форжа принимается (accept-new), сменившийся -- отказ,
    как всегда. Доверие то же, что у ssh-keyscan в плейбуке, но без курицы
    и яйца: `mop deploy` читает `.mop` проектов до плейбука, а сборщик и
    `mop project add` -- без него вовсе, и неизвестный хост валил их на
    «Host key verification failed». Свой ssh оператора не трогаем."""
    env = dict(environ)
    if "GIT_SSH_COMMAND" not in env and "GIT_SSH" not in env:
        env["GIT_SSH_COMMAND"] = "ssh -o StrictHostKeyChecking=accept-new"
    return env


def fetch(origin):
    """origin -> словарь манифестов проекта: {'project', 'asks', 'alien',
    'legacy', 'sandbox_vars', 'sandbox_tasks', 'bootstrap_vars',
    'bootstrap_tasks'} (пути или None).

    Нет файла — соответствующая часть None: большинству проектов хватает
    общего. Ошибки — громкие: недоступный origin и кривая форма поднимают
    RuntimeError, их переводит в выход вызывающий.

    Клон origin, а не рабочая копия: манифест принадлежит репозиторию, и
    origin — единственная его правда для deploy, у которого рабочей копии
    чужого проекта нет вовсе. Читается HEAD клона, то есть дефолтная ветка.

    Голый, глубины 1 и без блобов (#139): нужны два файла HEAD, а зеркало
    всей истории стоило rudesktop 77 с и 451 МБ -- `mop project add` висел
    на «reading .mop». `git show` догружает свой блоб сам.
    """
    project = puppets.project_of(origin)
    import tempfile
    with_dir = tempfile.mkdtemp(prefix=f"mop-mirror-{project}-")
    tmp = os.path.join(with_dir, "head.git")
    try:
        r = subprocess.run(["git", "clone", "--bare", "--depth", "1",
                            "--filter=blob:none", "-q", origin, tmp],
                           capture_output=True, text=True, env=clone_env(os.environ))
        if r.returncode != 0:
            raise RuntimeError(f"cannot read {project}: "
                               f"{(r.stderr or r.stdout).strip()}")

        def show(path):
            got = subprocess.run(["git", "-C", tmp, "show", f"HEAD:{path}"],
                                 capture_output=True, text=True,
                                 env=clone_env(os.environ))
            return got.stdout if got.returncode == 0 else None
        return _collect(project, show)
    finally:
        subprocess.run(["rm", "-rf", with_dir], capture_output=True)


def fetch_tree(root, project):
    """Те же манифесты из рабочей копии root: файлы как лежат, незакоммиченные
    тоже (#46). Ради этого сборка и заводилась в рабочей копии: правку `.mop`
    пробуют образом, а не влитием в master. Новой власти это не даёт — у того,
    кто собирает, и так ssh на узлы; тому, кто нет, `.mop` не поможет."""
    def show(path):
        p = os.path.join(root, path)
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return f.read()
    return _collect(project, show)


def _collect(project, show):
    """Словарь манифестов из источника show: path -> текст или None."""
    # base с PID: вырезка происходит и в deploy, и в сборке, и руками, и
    # один путь на всех однажды столкнул два клона в один tmp_pack (#32).
    # Файлы для ansible живут в base и переживают вызов — /tmp вычищается
    # перезагрузкой, мусор копится только до неё.
    base = f"/tmp/mop-manifest-{project}-{os.getpid()}"
    out = {"project": project, "asks": {}, "alien": [], "legacy": [],
           "sandbox_vars": None, "sandbox_tasks": None,
           "bootstrap_vars": None, "bootstrap_tasks": None, "bootstrap_text": None}

    def note_alien(names):
        out["alien"] += [k for k in names if k not in out["alien"]]

    # Песочница: новое имя, а за ним — старые, слитые в неё в том порядке, в
    # каком они играли: узловое прежде рабочего.
    s_vars, s_tasks = {}, []
    for path in (SANDBOX, *LEGACY):
        got = _parse(show(path), project, path)
        if got is None:
            continue
        if path in LEGACY:
            out["legacy"].append(path)
        mvars, tasks = got
        asks, mine, alien = parts(mvars)
        out["asks"].update(asks)
        s_vars.update(mine)
        note_alien(alien)
        s_tasks += tasks
    if s_vars:
        out["sandbox_vars"] = _write(base, project, "sandbox-vars.yml", None, s_vars)
    out["sandbox_tasks"] = _write(base, project, "sandbox-tasks.yml", s_tasks)

    # Bootstrap: размеров здесь не просят — они дело песочницы, и просьба
    # тут была бы проглочена молча, поэтому идёт в чужие, по имени.
    # Сырой текст -- тоже (#124): сервер принимает bootstrap глаголом `put`
    # текстом, и `mop project add` кладёт его туда сам, без прогона.
    out["bootstrap_text"] = show(BOOTSTRAP)
    got = _parse(out["bootstrap_text"], project, BOOTSTRAP)
    if got is not None:
        bvars, btasks = got
        asks, mine, alien = parts(bvars)
        note_alien(sorted(set(alien) | set(asks)))
        if mine:
            out["bootstrap_vars"] = _write(base, project, "bootstrap-vars.yml", None, mine)
        out["bootstrap_tasks"] = _write(base, project, "bootstrap-tasks.yml", btasks)
    out["alien"].sort()
    return out


def _parse(text, project, path):
    """Один манифест. -> (vars, tasks) или None, если файла нет."""
    if text is None:
        return None
    try:
        return play(text)
    except ValueError as e:
        raise RuntimeError(f"{project}/{path}: {e}")


def _write(base, project, name, tasks=None, of_vars=None):
    """Секция манифеста во временный файл для ansible; None, если пусто."""
    import yaml
    what = of_vars if of_vars is not None else tasks
    if not what:
        return None
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, name)
    with open(path, "w") as f:
        yaml.safe_dump(what, f, allow_unicode=True, default_flow_style=False)
    return path
