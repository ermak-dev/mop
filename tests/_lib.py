"""Общее проверок (#269): один счётчик, один прогон командлета, один
предохранитель от сети, одна подмена атрибутов и общие заглушки.

Не проверка: `mop dev test` и hermetic пропускают `_*.py`. Импортирует его
первым сам hermetic (#338): временный корень процесса обязан появиться раньше
дома hermetic. mop на импорте не трогает, поэтому порядок «hermetic раньше
mop» в файле проверок он не ломает.

До #269 в 43 файлах жило шесть диалектов одного и того же: замыкания check
разной формы, два стиля отчёта, пять копий «прогнать командлет в процессе»,
no_network в tests/cli.py, который импортировали другие проверки, и
десятки ручных save/restore в try/finally.
"""
import atexit
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types

# ─── временный корень процесса (#338) ────────────────────────────────────
# tempfile.mkdtemp проверок (дом hermetic, копия репозитория, каталоги самих
# проверок) не удалял никто: прогон на теле папета оставлял 276 каталогов и
# 376 МБ и за несколько прогонов заполнял tmpfs. Здесь -- один корень на
# процесс проверки: в него ведут tempfile.tempdir и TMPDIR (его наследуют
# дети -- командлеты, которых зовут проверки), и при выходе процесса он
# удаляется целиком. Вызовы mkdtemp в файлах проверок не трогаются. Корень
# живёт до конца процесса, так что проверка, заглядывающая в каталог после
# ребёнка, его видит.
RUN_ROOT = tempfile.mkdtemp(prefix="mop-test-run-")
tempfile.tempdir = RUN_ROOT
os.environ["TMPDIR"] = RUN_ROOT
atexit.register(shutil.rmtree, RUN_ROOT, ignore_errors=True)


# ─── счётчик ─────────────────────────────────────────────────────────────
class Checks:
    """Счётчик проверок файла. Провал печатается сразу строкой FAIL -- так,
    как печатали все прежние диалекты; итог -- report(имя) одной строкой и
    кодом выхода."""

    def __init__(self):
        self.cases = 0
        self.failures = []

    def check(self, what, ok, detail=""):
        """Утверждение: ok истинно. -> ok."""
        self.cases += 1
        if not ok:
            self._record(what, detail)
        return bool(ok)

    def expect(self, what, got, want):
        """Утверждение: got == want; провал показывает оба. -> совпало ли."""
        self.cases += 1
        if got != want:
            self._record(what, f"got {got!r}, want {want!r}")
            return False
        return True

    def fail(self, what, detail=""):
        """Провал, найденный вызывающим (цикл, исключение, разбор)."""
        self.cases += 1
        self._record(what, detail)

    def _record(self, what, detail):
        line = f"FAIL {what}" + (f": {detail}" if detail != "" else "")
        self.failures.append(line)
        print(line, flush=True)

    @property
    def failed(self):
        return len(self.failures)

    def report(self, name):
        """Итог файла одной строкой. -> код выхода: 0 -- всё прошло."""
        if self.failures:
            print(f"{name}: FAILED ({self.failed} of {self.cases})")
            return 1
        print(f"{name}: ok ({self.cases} checks)")
        return 0


# ─── командлет в процессе ────────────────────────────────────────────────
def run_command(fn, argv=(), stdin=None, via_cli=False, typed=None, module=None):
    """Прогнать командлет в процессе. -> (stdout, stderr, код выхода).

    fn -- его main. via_cli -- через mop.cli.run: ожидаемые отказы
    (RuntimeError, LookupError, ConnectionError) становятся строкой stderr и
    кодом 1, как у настоящего запуска; без него исключение уходит к
    вызывающему. SystemExit -- код выхода: число, либо текст (usage,
    sys.exit("...")) -- тогда код и есть этот текст, его печатает только
    интерпретатор на выходе. stdin -- текст для sys.stdin; typed -- ответы
    getpass по порядку, подменяются в module (его getpass)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    keep_stdin = sys.stdin
    answers = list(typed or ())
    keep_getpass = getattr(module, "getpass", None) if module is not None else None
    if stdin is not None:
        sys.stdin = io.StringIO(stdin)
    if module is not None and keep_getpass is not None:
        module.getpass = lambda prompt="": answers.pop(0)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                if via_cli:
                    from mop import cli
                    cli.run(fn, list(argv))
                else:
                    code = fn(list(argv)) or 0
            except SystemExit as e:
                code = e.code
    finally:
        sys.stdin = keep_stdin
        if module is not None and keep_getpass is not None:
            module.getpass = keep_getpass
    return out.getvalue(), err.getvalue(), code


# ─── сеть ────────────────────────────────────────────────────────────────
class NetworkGuard(Exception):
    """Проверка попыталась выйти в сеть."""


def no_network():
    """Предохранитель от живой шины для проверки, которая гоняет командлет
    через cli.run (#169). Ниже mop, поэтому его не обходит ни перезагрузка
    модулей, ни прежний bus в атрибуте пакета: socket.connect (им идёт и
    asyncio-соединение nats-py) и nats.connect настоящего пакета бросают;
    креды папета (MOP_BUS_CONFIG) убраны, сервер -- TEST-NET 192.0.2.1.
    -> функция отката."""
    import socket

    def refuse(*a, **k):
        raise NetworkGuard("the check tried to reach the network")

    real_nats = sys.modules.get("nats")
    saved = {"sock": socket.socket.connect, "sock_ex": socket.socket.connect_ex,
             "nats": getattr(real_nats, "connect", None),
             "env": {k: os.environ.get(k) for k in ("MOP_BUS_CONFIG", "MOP_SERVER_LAN",
                                                      "MOP_SERVER_DIR")}}
    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse
    if real_nats is not None:
        real_nats.connect = refuse
    os.environ.pop("MOP_BUS_CONFIG", None)
    os.environ.pop("MOP_SERVER_DIR", None)
    os.environ["MOP_SERVER_LAN"] = "192.0.2.1"

    def undo():
        socket.socket.connect = saved["sock"]
        socket.socket.connect_ex = saved["sock_ex"]
        if real_nats is not None:
            real_nats.connect = saved["nats"]
        for k, v in saved["env"].items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return undo


@contextlib.contextmanager
def offline():
    """no_network на время блока."""
    undo = no_network()
    try:
        yield
    finally:
        undo()


# ─── подмена атрибутов ───────────────────────────────────────────────────
_MISSING = object()


@contextlib.contextmanager
def patched(obj, **attrs):
    """Подменить атрибуты obj на время блока и вернуть прежние -- и в
    исключении. Атрибута не было -- после блока его снова нет."""
    saved = {k: getattr(obj, k, _MISSING) for k in attrs}
    try:
        for k, v in attrs.items():
            setattr(obj, k, v)
        yield obj
    finally:
        for k, v in saved.items():
            if v is _MISSING:
                if hasattr(obj, k):
                    delattr(obj, k)
            else:
                setattr(obj, k, v)


@contextlib.contextmanager
def patched_env(**values):
    """Переменные окружения на время блока; None -- снять."""
    saved = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def restored(obj, *names):
    """Вернуть атрибуты obj после блока, как бы их ни меняли внутри."""
    saved = {k: getattr(obj, k, _MISSING) for k in names}
    try:
        yield obj
    finally:
        for k, v in saved.items():
            if v is _MISSING:
                if hasattr(obj, k):
                    delattr(obj, k)
            else:
                setattr(obj, k, v)


# ─── общие заглушки ──────────────────────────────────────────────────────
class Msg:
    """Сообщение шины: subject, data (bytes), ответы -- в replies, разобранные
    из JSON. body -- то же, что data, но словарём."""

    def __init__(self, subject="", data=b"", body=None, reply=""):
        self.subject, self.reply = subject, reply
        self.data = json.dumps(body).encode() if body is not None else data
        self.replies = []

    async def respond(self, data):
        self.replies.append(json.loads(data))


def canned(out="", code=0, calls=None):
    """async sh/bsh, который отвечает (out, code) на любой вызов; calls --
    список, куда пишутся аргументы вызовов."""
    async def fake(*a, **k):
        if calls is not None:
            calls.append(a)
        return out, code
    return fake


async def bash(name, script, timeout=20):
    """bsh на настоящем bash: скрипт исполняется, как в теле."""
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return r.stdout + r.stderr, r.returncode


def udp_socket(local, unresolvable=(), gaierror=None):
    """Модуль socket без сети для выбора маршрута UDP: connect запоминает
    адрес, getsockname отвечает local; имя из unresolvable -- gaierror.
    -> (заглушка модуля, список адресов, куда «подключались»)."""
    import socket as real
    targets = []

    class Sock:
        def __init__(self, *a):
            pass

        def connect(self, addr):
            if addr[0] in unresolvable:
                raise real.gaierror(-2, "Name or service not known")
            targets.append(addr)

        def getsockname(self):
            return (local, 40000)

        def close(self):
            pass

    stub = types.SimpleNamespace(socket=Sock, AF_INET=real.AF_INET,
                                 SOCK_DGRAM=real.SOCK_DGRAM, gaierror=real.gaierror,
                                 error=real.error)
    return stub, targets


# ─── таблица ворот (#267) ────────────────────────────────────────────────
GATE_NOW = 1_800_000_000


def gate_table_267():
    """Таблица ворот (#267): (владелец, вызывающий, клон, force) -> все
    сочетания. -> [(name, clone, caller, force)]. Одна на tests/cluster.py и
    tests/agent.py: обе стороны сверяются с одной таблицей, а проверка не
    импортирует другую (#269). mop -- лениво: _lib не трогает его на импорте."""
    from mop.common.domain import CloneFacts, Owner
    owners = (None, Owner("alice", GATE_NOW - 10), Owner("alice", GATE_NOW - 100_000))
    work = ({"dirty": 0, "ahead": 0}, {"dirty": 2, "ahead": 0}, {})
    table = []
    for owner in owners:
        for w in work:
            # По именам (#273): позиционный вызов пережил вставку поля home
            # (#272) и с тех пор клал origin в home, dirty в origin и
            # владельца в ahead -- таблица сверяла обе стороны на мусоре.
            clone = CloneFacts(branch="master", default_branch="master",
                               origin="git@x:a/mop.git", dirty=w.get("dirty"),
                               ahead=w.get("ahead"), owner=owner)
            for caller in ("alice", "bob", None):
                for force in (False, True):
                    table.append(("pu-mop-1", clone, caller, force))
    return table


# ─── поддельный Nomad (#275) ─────────────────────────────────────────────
class NoLiveNomad:
    """Подмена модуля nomad, которая бросает на любом вызове (#375): путь,
    обещавший ходить в Nomad через api, не должен дотянуться до живого
    модуля. Исключения и константы модуля ей не нужны -- их зовут только на
    отказе."""

    def __getattr__(self, name):
        def call(*a, **kw):
            raise AssertionError(f"live nomad.{name} called past the api")
        return call


class FakeNomad:
    """nomad.NomadApi без сети: таблицы вместо кластера, вызовы -- в calls.

    Передаётся сервису кластера и сборке образа параметром (cluster.using,
    answer(api=...), image.clear/restore(api=...)), а не подменой атрибутов
    модуля nomad. Каждая функция протокола здесь есть: isinstance с
    runtime_checkable протоколом смотрит именно на это."""

    def __init__(self, jobs=None, allocs=None, nodes=None, meta=None):
        self.jobs = dict(jobs or {})          # {ID: job}
        self.allocs = dict(allocs or {})      # {job ID: последняя аллокация}
        self.nodes = list(nodes or [])        # сводки узлов, как get_nodes
        self.meta = dict(meta or {})          # {узел: мета}
        self.calls = []

    def _call(self, *what):
        self.calls.append(what)

    # джобы
    def get_job(self, job_id):
        self._call("get_job", job_id)
        return self.jobs.get(job_id)

    def get_jobs(self, prefix, meta=False):
        self._call("get_jobs", prefix)
        return [j for i, j in sorted(self.jobs.items()) if i.startswith(prefix)]

    def register(self, spec):
        self._call("register", spec)
        job = spec.get("Job") or spec
        self.jobs[job.get("ID")] = job

    def deregister(self, job_id, purge=True):
        self._call("deregister", job_id, purge)

    # аллокации
    def latest_alloc(self, job_id):
        self._call("latest_alloc", job_id)
        return self.allocs.get(job_id)

    def alloc_restart(self, alloc_id):
        self._call("alloc_restart", alloc_id)

    def alloc_stderr(self, alloc_id, task, tail=8000):
        self._call("alloc_stderr", alloc_id, task)
        return ""

    def alloc_stop(self, alloc_id):
        self._call("alloc_stop", alloc_id)

    # узлы
    def get_nodes(self):
        self._call("get_nodes")
        return list(self.nodes)

    def node_capacity(self, node_summary):
        self._call("node_capacity", node_summary.get("Name"))
        return 0, 0

    def node_allocs(self, node_name):
        self._call("node_allocs", node_name)
        return []

    def node_drain(self, node_name, deadline=300):
        self._call("node_drain", node_name)

    def node_eligibility(self, node_name, eligible):
        self._call("node_eligibility", node_name, eligible)

    def node_forget(self, node_name):
        self._call("node_forget", node_name)

    def node_summary(self, node_name):
        self._call("node_summary", node_name)
        return next((n for n in self.nodes if n.get("Name") == node_name), None)

    def node_meta(self, node_name):
        self._call("node_meta", node_name)
        return dict(self.meta.get(node_name) or {})

    def nodes_meta(self):
        self._call("nodes_meta")
        return {n: dict(m) for n, m in self.meta.items()}

    def node_dynamic_meta(self, node_name):
        self._call("node_dynamic_meta", node_name)
        return dict(self.meta.get(node_name) or {})

    def set_node_meta(self, node_name, updates):
        self._call("set_node_meta", node_name, dict(updates))
        self.meta.setdefault(node_name, {}).update(updates)
