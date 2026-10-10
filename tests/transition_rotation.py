#!/usr/bin/env python3
"""Сквозные проверки перехода ротации ключа и посева реестра:
python3 tests/transition_rotation.py

#541, фаза B2 (приёмочные проверки сшивок уже влитых частей): ротация
клиентского ключа LLM-прокси (#538 библиотека, #539 адаптер и CLI) и
посев реестра на пустой сервер (#540, отказ стейджинга #548). Проверяется
не шаг машины по отдельности (это tests/proxykey.py и tests/proxyapi.py)
и не атомарность входа (tests/connection.py), а СТЫКИ между частями,
которые в юнит-проверках не встречаются:

  S1  два писателя secrets.env -- доставка ротации (Effects.deliver) и
      тикер (push): один путь paths.NODE_SECRETS, байт-в-байт один блоб
      одной строки MOP_PROXY_KEY, sha-гейт тика молчит на втором прогоне;
  S2  выданный после ротации ключ через server._client_config ->
      login.complete (проба только заглушкой, без HTTP) -> connection.json
      -> session_env; окно old+new -- по списку фейкового транспорта и
      пробе старого ключа; отозванный чужой рукой старый ключ держит
      ротацию открытой ДО выдачи;
  S3  круглый ход посева: снимок контроллера -> стейджинг -> тот же вид
      writer'а и reader'а на посеянном файле, права, путь без содержимого;
  S4  отказ стейджинга (RuntimeError, вариант #548 поверх OSError) не
      валит deploy: одна строка stderr, плейбук идёт с признаком.

Как и в фазе B1 (tests/transition_registry.py): свойство уже влито --
приёмочные зелёные сразу, а доказательство их силы -- матрица порч
(check_tamper_matrix_541): под порчей прогон краснеет, после отката --
снова зелёный.

Герметичность: временные каталоги, настоящий реестр и состояния не
читаются и не пишутся, сеть не трогается: проба логина -- заглушка,
транспорт адаптера -- фейк, шина подменена на границе bus.request_many.
"""
import json
import os
import shutil
import stat
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, offline, patched, patched_env, run_command  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.cli.core import _common  # noqa: E402
from mop.common import bus, creds, llm, paths, projects  # noqa: E402
from mop.server import proxykey  # noqa: E402

HOST = "srv.test"
OLD = "old-key-541"
PANEL = "panel-key-541"


class FakeHTTP:
    """Транспорт адаптера на границе urllib (#539): без сети, с журналом.

    GET api-keys -> список; PUT -> применяется целиком; /v1/models?key ->
    200, когда ключ в списке, иначе 401. PUT сохраняет объединение -- так
    ведёт себя панель v8, чужие ключи не трогает."""

    def __init__(self, keys):
        self.keys = list(keys)
        self.log = []

    def get_keys(self, bearer):
        self.log.append(("get", bearer))
        return list(self.keys)

    def put_keys(self, bearer, body):
        self.log.append(("put", bearer, list(body)))
        self.keys = list(body)
        return None

    def models(self, key):
        self.log.append(("probe", key))
        return 200 if key in self.keys else 401


class RetiredOldHTTP(FakeHTTP):
    """PUT применяется, но старый ключ уже отозван (чужая рука панели).

    Pre-PUT пробы старого ещё проходят, post-PUT -- нет: сценарий «окно
    закрылось до раздачи», который ловит probe-шаг библиотеки."""

    def __init__(self, keys):
        super().__init__(keys)
        self.put_done = False

    def put_keys(self, bearer, body):
        out = super().put_keys(bearer, body)
        self.put_done = True
        return out

    def models(self, key):
        self.log.append(("probe", key))
        if key == OLD and self.put_done:
            return 401
        return 200 if key in self.keys else 401


def stand():
    """Временный контроллер ротации: источник, копия кластера, ключ
    management, пути состояния операции и тика. Оба файла ключа -- OLD,
    режим 0600, владелец текущий (как требует _serviceable)."""
    root = tempfile.mkdtemp(prefix="mop-541-rot-")
    box = {"root": root, "op": os.path.join(root, "op.json"),
           "lock": os.path.join(root, "rot.lock"),
           "state": os.path.join(root, "push.json")}
    for name in ("src", "cp"):
        p = os.path.join(root, name + ".pass")
        with open(p, "w", encoding="utf-8") as f:
            f.write(OLD + "\n")
        os.chmod(p, 0o600)
        box[name] = p
    box["mgmt"] = os.path.join(root, "mgmt.pass")
    with open(box["mgmt"], "w", encoding="utf-8") as f:
        f.write("mgmt-key-541\n")
    return box


def rotate(box, http, writes):
    """Полная ротация настоящим адаптером: begin + resume, доставка --
    реальный Effects.deliver через подменённый bus.request_many (сбор
    писем шины в writes). -> ответ resume."""
    from mop.server import proxyapi
    eff = proxyapi.Effects(transport=http, mgmt_file=box["mgmt"])

    def fake_write(verb, nodes, timeout=None, channel=None, project=None,
                   files=None, **fields):
        writes.append((verb, list(nodes), files))
        return {n: {"written": [os.path.basename(paths.NODE_SECRETS)]}
                for n in nodes}

    # Стойло -- установка с настоящим сертификатом (#557: MOP_TLS_CERT
    # задан, частной CA нет): корня в блобе нет, ca_source молчит.
    with patched(proxykey, OP=box["op"], LOCK=box["lock"]), \
            patched_env(MOP_TLS_CERT="/etc/certs/real.pem"), \
            patched(bus, request_many=fake_write):
        proxykey.begin(box["src"], box["cp"], ["node1"], exclusive=True)
        return proxykey.resume(exclusive=True, effects=eff)


def new_key_of(box):
    """Новый ключ завершённой операции из её приватного состояния."""
    with open(box["op"], encoding="utf-8") as f:
        return json.load(f)["new"]


def ok_answers(nodes):
    """Ответ агента write -- словарь written (#135), строки сводит шина."""
    return {n: {"written": [os.path.basename(paths.NODE_SECRETS)]}
            for n in nodes}


# ── S1: два писателя secrets.env -- ротация и тикер ──────────────────────
# HYPOTHESIS (#541 B2): у secrets.env два писателя -- доставка шага ротации
# и тикер сервиса (push). Разошедшийся формат или путь молча оставил бы
# узлы с ключом, который прокси больше не принимает (или наоборот). Оба
# обязаны писать paths.NODE_SECRETS одним рендером proxykey.blob, и тикер
# после ротации раздаёт тот же ключ, что записал _local_step, ровно один
# раз -- до следующей смены (sha-гейт).
def check_rotation_writers_541(c):
    try:
        _s1_runs(c)
    except Exception as e:  # noqa: BLE001 -- проверка ловит, не глотает
        c.fail(f"#541 S1 the acceptance escaped: {e!r}")


def _s1_runs(c):
    from mop.common import puppets as puppets_mod
    box = stand()
    try:
        writes = []
        out = rotate(box, FakeHTTP([OLD, PANEL]), writes)
        if not c.check("#541 S1 the full rotation completes",
                       out.get("ok") and out.get("stage") == "completed", out):
            return
        new = new_key_of(box)
        for name in ("src", "cp"):
            with open(box[name], encoding="utf-8") as f:
                got = f.read()
            c.expect(f"#541 S1 the rotation wrote the new key to {name}",
                     got, new + "\n")
        if not c.check("#541 S1 the rotation delivered exactly once",
                       len(writes) == 1 and writes[0][0] == "write",
                       [w[0] for w in writes]):
            return
        rot_files = writes[0][2]
        c.check("#541 S1 the rotation writes the node secrets path",
                rot_files and rot_files[0][0] == paths.NODE_SECRETS, rot_files)
        rot_content = bus.file_data(rot_files[0][1]) if rot_files else b""

        # Тикер после ротации: источник -- тот же файл, что писал
        # _local_step; раздача та же, второго письма sha-гейт не пускает.
        sent = []

        def push_write(verb, nodes, timeout=None, channel=None, project=None,
                       files=None, **fields):
            sent.append((verb, list(nodes), files))
            return ok_answers(nodes)

        with patched(proxykey, KEY_FILE=box["src"], STATE=box["state"]), \
                patched_env(MOP_TLS_CERT="/etc/certs/real.pem"), \
                patched(puppets_mod, ready_nodes=lambda: ["node1"]), \
                patched(bus, request_many=push_write):
            first = proxykey.push(env={"MOP_PROXY_KEY": "stale-env-541"})
            second = proxykey.push(env={"MOP_PROXY_KEY": "stale-env-541"})
        c.check("#541 S1 the ticker push distributes the rotated key",
                first == {"node1": "OK"}, first)
        c.check("#541 S1 the sha gate silences the second push",
                second is None and len(sent) == 1, (second, len(sent)))
        push_files = sent[0][2] if sent else []
        c.check("#541 S1 the ticker writes the same node secrets path",
                push_files and push_files[0][0] == paths.NODE_SECRETS, push_files)
        push_content = bus.file_data(push_files[0][1]) if push_files else b""
        # Буквальная форма -- не «равенство двух писателей друг другу»
        # (при общем повреждении оба разошлись бы одинаково), а контракт
        # блоба #557: строка MOP_PROXY_KEY=<ключ>, затем -- когда сервер
        # пинит цепочку -- ровно одна строка MOP_PROXY_CA=<base64 PEM>, и
        # ничего больше. Проверка гоняет стойло без корня (MOP_TLS_CERT
        # задан, файлов нет): ca_source() здесь возвращает None, блоб --
        # одна строка ключа.
        want = f"MOP_PROXY_KEY={new}\n".encode()
        c.check("#541 S1 both writers send one byte-identical canonical blob",
                rot_content == push_content == want,
                (rot_content, push_content, want))
        c.check("#541 S1 the blob is one MOP_PROXY_KEY line with no root",
                rot_content.count(b"MOP_PROXY_KEY") == 1
                and b"MOP_PROXY_CA" not in rot_content
                and rot_content.endswith(b"\n")
                and b"\n" not in rot_content[:-1], rot_content)
    finally:
        shutil.rmtree(box["root"], ignore_errors=True)


# ── S2: выданный ключ через _client_config -> login -> session_env ───────
# HYPOTHESIS (#541 B2): после завершения ротации установленный ключ -- тот,
# что лежит в источнике (новый). Сервер раздаёт его клиенту (_client_config
# читает proxykey.source от KEY_FILE), вход проверяет его пробой и кладёт в
# connection.json, session_env поднимает его в ANTHROPIC_AUTH_TOKEN. Окно
# old+new -- штатное состояние после PUT (список хранит объединение, старый
# ключ отзывают отдельно), и проба старого ключа по фейковому транспорту
# обязана это показывать; отозванный ДО раздачи старый ключ держит операцию
# открытой: distribution would strand nodes.
def check_client_rotation_541(c):
    try:
        _s2_runs(c)
    except Exception as e:  # noqa: BLE001 -- проверка ловит, не глотает
        c.fail(f"#541 S2 the acceptance escaped: {e!r}")


def _s2_runs(c):
    from mop.cli.pool import login as join
    from mop.server import cluster, proxyapi
    box = stand()
    servers = tempfile.mkdtemp(prefix="mop-541-s2-")
    try:
        writes = []
        http = FakeHTTP([OLD, PANEL])
        out = rotate(box, http, writes)
        if not c.check("#541 S2 the rotation completes before the client asks",
                       out.get("ok"), out):
            return
        new = new_key_of(box)

        # Окно old+new: список фейкового транспорта хранит объединение
        # (чужой panel-ключ не тронут), старый ключ всё ещё принят,
        # заведомо неверный -- отвергнут.
        c.check("#541 S2 the PUT kept the union of old, new and the panel key",
                set(http.keys) == {OLD, new, PANEL}, http.keys)
        eff = proxyapi.Effects(transport=http, mgmt_file=box["mgmt"])
        c.check("#541 S2 the window holds: the old key still probes true",
                eff.probe(OLD) is True, None)
        c.check("#541 S2 the new key probes true", eff.probe(new) is True, None)
        c.check("#541 S2 a definitely wrong key probes false",
                eff.probe("definitely-wrong-541") is False, None)

        # Сервер раздаёт ключ установленного прокси: честное чтение
        # источника ротации, без заглушки source.
        with patched(proxykey, KEY_FILE=box["src"]), \
                patched_env(MOP_SERVER_LAN=HOST, MOP_HTTPS_PORT="443"):
            served = cluster._client_config({"_caller": "alice"})
            anonymous = cluster._client_config({})
        c.check("#541 S2 the server hands out the rotated key",
                served.get("ok") is True and served.get("proxy_key") == new
                and served.get("proxy_url") == f"https://{HOST}:443", served)
        c.check("#541 S2 client_config refuses an anonymous subject",
                anonymous.get("error") is not None, anonymous)

        # Вход: проба -- только заглушка (никакого HTTP), запись -- настоящий
        # writer; проверяется, что пробе и записи достался новый ключ.
        dest = os.path.join(servers, HOST)
        probes = []

        def probe_stub(url, key, directory, port=None):
            probes.append((url, key, directory, port))

        with patched(join, _still_valid=lambda *a: False,
                     _login=lambda *a: "bus-alice-541",
                     _fetch=lambda *a: {"https_port": served.get("https_port"),
                                        "proxy_url": served.get("proxy_url"),
                                        "proxy_key": served.get("proxy_key")},
                     _probe=probe_stub):
            join.complete(HOST, "alice", dest, "443")
        c.check("#541 S2 the login probes the issued url with the rotated key",
                probes == [(served.get("proxy_url"), new, dest, "443")], probes)
        kind, profile = creds.snapshot(dest)
        c.check("#541 S2 connection.json carries the rotated key",
                kind == "human" and profile.get("proxy_key") == new,
                (kind, profile))
        with patched(creds, ROOT=servers), \
                patched_env(MOP_SERVER_LAN=HOST, MOP_HTTPS_PORT="443",
                            MOP_SERVER_DIR=None):
            env = _common.session_env()
        c.check("#541 S2 session_env starts on the rotated key",
                env.get(llm.AUTH_VAR) == new
                and env.get("ANTHROPIC_BASE_URL") == f"https://{HOST}:443",
                (env.get(llm.AUTH_VAR), env.get("ANTHROPIC_BASE_URL")))

        # Старый ключ умирает чужой рукой ПОСЛЕ записи списка (PUT уже
        # применился): probe-шаг ловит («no longer accepts ... before the
        # operation issued anything»), чекпоинт стоит в listed, файлы не
        # тронуты, раздачи не было -- иначе узлы остались бы со старым
        # ключом, который прокси больше не принимает. Отзыв имитируем на
        # транспортном уровне (models старого -- 401 после PUT).
        stranded = stand()
        try:
            writes2 = []
            out2 = rotate(stranded, RetiredOldHTTP([OLD, PANEL]), writes2)
            c.check("#541 S2 a retired old key blocks the rotation before issuing",
                    not out2.get("ok")
                    and "no longer accepts the old key" in str(out2.get("blocker", "")),
                    out2)
            with open(stranded["op"], encoding="utf-8") as f:
                stage = json.load(f).get("stage")
            c.expect("#541 S2 the checkpoint stays at listed", stage, "listed")
            with open(stranded["src"], encoding="utf-8") as f:
                src_now = f.read()
            c.check("#541 S2 the source keeps the old key",
                    src_now == OLD + "\n", src_now)
            c.check("#541 S2 nothing was delivered", not writes2, writes2)
        finally:
            shutil.rmtree(stranded["root"], ignore_errors=True)
    finally:
        shutil.rmtree(box["root"], ignore_errors=True)
        shutil.rmtree(servers, ignore_errors=True)


# ── S3: круглый ход посева реестра ────────────────────────────────────────
# HYPOTHESIS (#541 B2): снимок, подготовленный на контроллере (#540),
# будучи посеянным, -- тот же реестр: strict-вид writer'а и tolerant-вид
# reader'а на каталоге посева равны виду контроллера, файл закрыт, а
# плейбуку едет путь без содержимого. Порча-write_snapshot с потерей
# лимитов ловится несходством вида.
def check_seed_roundtrip_541(c):
    try:
        _s3_runs(c)
    except Exception as e:  # noqa: BLE001 -- проверка ловит, не глотает
        c.fail(f"#541 S3 the acceptance escaped: {e!r}")


def _s3_runs(c):
    from mop.cli.server import _play
    from mop.cli.server import deploy as deploy_cli
    root = tempfile.mkdtemp(prefix="mop-541-seed-")
    home = os.path.join(root, "home")
    reg = os.path.join(home, ".config", "mop")
    os.makedirs(reg)
    with open(os.path.join(reg, "projects"), "w", encoding="utf-8") as f:
        f.write("git@h:ermak/mop.git\n")
    with open(os.path.join(reg, "limits.json"), "w", encoding="utf-8") as f:
        f.write('{"mop": 3}')
    was = os.environ.get("HOME")
    was_home = os.environ.get("MOP_HOME")
    was_dir = projects.SNAPSHOT_DIR
    got = None
    try:
        os.environ["HOME"] = home
        os.environ["MOP_HOME"] = "/srv/mop-541"
        projects.SNAPSHOT_DIR = reg
        got = deploy_cli._staged_registry_seed()
        if not c.check("#541 S3 staging produces a seed file",
                       isinstance(got, str) and os.path.isfile(got), got):
            return
        mode = stat.S_IMODE(os.stat(got).st_mode)
        c.check("#541 S3 the staged snapshot is private", mode == 0o600, oct(mode))
        dmode = stat.S_IMODE(os.stat(os.path.dirname(got)).st_mode)
        c.check("#541 S3 the staging directory is private", dmode == 0o700, oct(dmode))
        want = projects.snapshot_of(reg)
        seeded_dir = os.path.dirname(got)
        c.check("#541 S3 the seeded server sees the controller's writer view",
                projects.snapshot_of(seeded_dir) == want, projects.snapshot_of(seeded_dir))
        c.check("#541 S3 the seeded server sees the controller's reader view",
                projects.view_of(seeded_dir) == projects.view_of(reg),
                projects.view_of(seeded_dir))
        vars_ = _play.play_vars([], registry_seed=got)
        c.check("#541 S3 the play carries the seed as a path, not a payload",
                any(f'"mop_registry_seed": "{got}"' in v for v in vars_)
                and not any("git@h" in v for v in vars_), vars_)
    finally:
        os.environ["HOME"] = was
        if was_home is None:
            os.environ.pop("MOP_HOME", None)
        else:
            os.environ["MOP_HOME"] = was_home
        projects.SNAPSHOT_DIR = was_dir
        if isinstance(got, str):
            shutil.rmtree(os.path.dirname(got), ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


# ── S4: отказ стейджинга (RuntimeError) не валит deploy ──────────────────
# HYPOTHESIS (#541 B2): #548 закрыл трейсбек OSError; вариант RuntimeError
# (непредставимый реестр) -- тот же контракт: одна строка stderr, плейбук
# идёт с признаком mop_registry_seed_refused, код ноль. Валило бы -- сервер
# с реестром не разворачивался бы из-за чужого контроллера.
def check_staging_refusal_541(c):
    try:
        _s4_runs(c)
    except Exception as e:  # noqa: BLE001 -- проверка ловит, не глотает
        c.fail(f"#541 S4 the acceptance escaped: {e!r}")


# Вывод ansible-inventory на чистом инвентаре (форма -- как в tests/cli.py,
# #186): хост без своих переменных есть только в списке группы.
INVENTORY = {
    "_meta": {"hostvars": {
        "explicit": {"mop_driver": "host"},
        "hyper": {"mop_driver": "pve", "mop_pve_vmid_base": "9000"},
        "localhost": {"ansible_connection": "local"},
        "typo": {"mop_driver": "pve"}}},
    "all": {"children": ["ungrouped", "server", "puppet", "proxmox"]},
    "proxmox": {"hosts": ["hyper"]},
    "puppet": {"hosts": ["plain", "explicit", "hyper", "typo"]},
    "server": {"hosts": ["localhost"]},
}


def _s4_runs(c):
    import subprocess
    from mop.cli.server import _play
    from mop.cli.server import deploy
    from mop.common import config, projects
    from mop.server import identity
    d = tempfile.mkdtemp(prefix="mop-541-s4-")
    inventory, key = os.path.join(d, "inventory.yaml"), os.path.join(d, "id")
    for f in (inventory, key, key + ".pub"):
        open(f, "w").close()
    people = os.path.join(d, "operators")
    with open(people, "w", encoding="utf-8") as f:
        f.write(identity.format_line(identity.Identity("anton", "admin", ("*",)),
                                     identity.hash_password("x")) + "\n")
    plays = []
    listing = [INVENTORY]
    env = dict(os.environ)

    def run_mock(argv):
        """Игра (#475): ansible-inventory отвечает листингом, плейбук -- нулём."""
        text = json.dumps(listing[0]) if argv and argv[0] == "ansible-inventory" else ""
        return subprocess.CompletedProcess(argv, 0, text, "")

    try:
        with offline(), \
                patched(shutil, which=lambda cmd: f"/usr/bin/{cmd}"), \
                patched(config, require=lambda *a: None), \
                patched(subprocess, run=lambda argv, **kw: run_mock(argv)), \
                patched(_play, play=lambda *a, **kw: plays.append(kw) or (0, "")), \
                patched(deploy, missing_extras=lambda setting, root: [],
                        manifests=lambda origins: {},
                        check=lambda: None,
                        _staged_registry_seed=lambda: (_ for _ in ()).throw(
                            RuntimeError("registry seed staging failed: "
                                         "the pair carries a bare name"))), \
                patched(bus, ask_cluster=lambda *a, **kw: {"ok": True, "projects": []}), \
                patched(projects, for_deploy=lambda answer, local: ([], None),
                        read=lambda: []), \
                patched(creds, collect=lambda *a, **kw: [],
                        operator=lambda dest: "anton",
                        snapshot=lambda dest: ("human", {"user": "anton",
                                                         "password": "x"})):
            os.environ.update({"INVENTORY": inventory, "MOP_GIT_KEY": key,
                               "MOP_OPERATORS_FILE": people})
            try:
                out, err, code = run_command(deploy.main, [])
            except RuntimeError as e:
                # До #548/#540 отказ подготовки поднимался из main -- здесь
                # это провал проверки, а не сценарий.
                c.fail(f"#541 S4 the refusal escaped main: {e!r}")
                out, err, code, plays[:] = "", "", 1, []
            sent = plays[-1] if plays else {}
            c.check("#541 S4 a RuntimeError in staging does not kill deploy",
                    code == 0 and plays and "Traceback" not in err,
                    (code, repr(err)[:200]))
            lines = [l for l in err.strip().splitlines() if l.strip()]
            c.check("#541 S4 the refusal is one stderr line with the reason",
                    len(lines) == 1 and "cannot stage the registry seed" in err
                    and "bare name" in err, repr(err)[:200])
            c.check("#541 S4 the play runs with the refused flag and no seed",
                    sent.get("registry_seed") is None
                    and sent.get("registry_seed_refused") is True, sent)
    finally:
        os.environ.clear()
        os.environ.update(env)
        shutil.rmtree(d, ignore_errors=True)


# ── матрица порч: каждая приёмочная ловит свою порчу красным ─────────────
# Как в B1 (#541, вариант (а) ревью): порча поведения -- monkey-атрибут
# модуля, приёмочная гоняется в том же процессе (дочерний импортировал бы
# mop заново и патча не увидел), прогон под порчей обязан краснеть, после
# отката -- снова зелёный. Так доказательство силы проверки живёт в CI,
# а не в комментарии.
def _runs_red_541(what, fn):
    """Прогон приёмочной fn со своим Checks() и заглушённым выводом --
    глушит общий run_command из _lib (#269: своего диалекта нет).
    -> (красный?, строки провалов)."""
    cc = Checks()
    _, _, _ = run_command(lambda _argv: fn(cc))
    return cc.failed, cc.failures


def _tamper_541(c, what, patch, check):
    """Одна порча: под patch приёмочная check обязана краснеть, без порчи --
    снова зелёная."""
    with patch:
        red, lines = _runs_red_541(what, check)
    c.check(f"#541 {what}: the acceptance catches the tamper red", red > 0,
            "; ".join(lines)[:300])
    red2, lines2 = _runs_red_541(what, check)
    c.check(f"#541 {what}: the acceptance is green again once restored",
            red2 == 0, "; ".join(lines2)[:300])


def check_tamper_matrix_541(c):
    """Матрица порч фазы B2 (каждая проверена красным прогоном здесь же):

      1. доставка мимо единственного рендера blob -- ловит S1 (буквальная
         форма блоба);
      2. полушаг local (копия кластера осталась со старым ключом) --
         ловит S1 (запись нового в оба пути);
      3. source() тика отвечает окружению, а не файлу -- ловит S1
         (байт-в-байт совпадение двух писателей);
      4. сервер раздаёт доротационный ключ -- ловит S2 (проба, профиль,
         session_env);
      5. probe-шаг слеп к умирающему старому ключу -- ловит S2 (блокер
         отозванного ключа до выдачи);
      6. write_snapshot-«нормализатор» теряет лимиты -- ловит S3
         (несходство вида writer'а);
      7. отказ стейджинга снова валит deploy (fail -> выход) -- ловит S4.
    """
    def hand_blob(env):
        key = env.get("MOP_PROXY_KEY")
        if isinstance(key, tuple):
            key = key[0]
        return f"proxy key: {key}\n"

    _tamper_541(c, "a delivery bypassing the one renderer",
                patched(proxykey, blob=hand_blob), check_rotation_writers_541)

    real_local = proxykey._local_step

    def half_local(state, seams):
        out = real_local(state, seams)
        if out.get("ok"):
            with open(state["copy"], "w", encoding="utf-8") as f:
                f.write(state["old"] + "\n")
        return out

    _tamper_541(c, "a half-written local step leaves the cluster copy stale",
                patched(proxykey, _local_step=half_local),
                check_rotation_writers_541)

    _tamper_541(c, "the ticker source answering the stale env key",
                patched(proxykey, source=lambda env, path=None: "stale-env-541"),
                check_rotation_writers_541)

    _tamper_541(c, "the server handing out the pre-rotation key",
                patched(proxykey, source=lambda env, path=None: OLD),
                check_client_rotation_541)

    def lax_probe_step(state, seams):
        if not seams.probe(state["new"]):
            return {"ok": False, "blocker": "pending: the proxy does not "
                    "accept the new key yet"}
        state["stage"] = "accepted"
        proxykey._save_op(state)
        return {"ok": True, "stage": "accepted"}

    _tamper_541(c, "a probe step blind to the dying old key",
                patched(proxykey, _probe_step=lax_probe_step),
                check_client_rotation_541)

    real_write = projects.write_snapshot

    def dropping_write(snapshot, path=projects.SNAPSHOT):
        trimmed = {"version": snapshot["version"],
                   "projects": [{k: v for k, v in p.items() if k != "limit"}
                                for p in snapshot["projects"]]}
        return real_write(trimmed, path=path)

    _tamper_541(c, "a seed writer dropping the limits",
                patched(projects, write_snapshot=dropping_write),
                check_seed_roundtrip_541)

    from mop.cli import lib

    def fatal_fail(text):
        print(text, file=sys.stderr, flush=True)
        raise SystemExit(1)

    _tamper_541(c, "a staging refusal killing deploy again",
                patched(lib, fail=fatal_fail), check_staging_refusal_541)


def main():
    c = Checks()
    check_rotation_writers_541(c)
    check_client_rotation_541(c)
    check_seed_roundtrip_541(c)
    check_staging_refusal_541(c)
    check_tamper_matrix_541(c)
    return c.report("transition_rotation")


if __name__ == "__main__":
    sys.exit(main())
