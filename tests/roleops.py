#!/usr/bin/env python3
"""Фронтенды управления ролями #618 на каноническом пути #642/#793.

HYPOTHESIS (#618): командлеты add/update/recycle и их MCP-объявления ролей
не знают. Грант-мастер (#582) обязан звать add с ролью и БЕЗ клиентского
workspace; старый сервер молча завёл бы безролевого папета.
SOLUTION (#793): свойства #618 переведены на канонический шов #642 --
единственный transient HTTPS consumer `_control.invoke` (та же поверхность,
что и tests/control_consumers.py). Роль на проводе -- approved immutable
`definition_id`, ключ запроса -- `request_id`, клиентской политики в
payload нет вовсе. Проверки гоняют реальный разбор `_control.invoke` и
реальный `mop.client.control.Client` поверх TLS-двойника `_control_fixture`;
legacy-оркестрация (ops.add/ops.update/puppets.recycle, context.here,
workspace_text) подменена отказом -- канонический путь обязан её не касаться.
Update и recycle admission в этом релизе НЕ связаны (`_control.GAPS`):
сценарии, где свойство жило только на старом пути, сохранены явным
unsupported-контрактом с точным пользовательским отказом; список -- в
отчёте #793.
STATUS: FIXED — see #793
"""
import contextlib
import json
import os
import sys
import uuid

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, patched_env, run_command  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from _control_fixture import (DEFINITION, EPOCH, Fixture, KEY, NODE, OP,  # noqa: E402
                              PROJECT, PUPPET, Response, TOKEN, create_operation)
from mop import cli  # noqa: E402
from mop.cli import _control  # noqa: E402
from mop.cli.core import add as cli_add  # noqa: E402
from mop.cli.core import _common  # noqa: E402
from mop.client import control as api  # noqa: E402
from mop.common import context, ops, puppets  # noqa: E402

API = "https://controller.example"
# Полный явный вход канонического add: проект, узел, роль, эпоха, ключ, срок.
CREATE = [PROJECT, "--node-id", NODE, "--definition-id", DEFINITION,
          "--epoch", EPOCH, "--request-id", KEY, "--not-after",
          "2026-10-16T10:05:00Z"]
PLAY = "- name: greet\n  tasks: []\n"
NOT_FOUND = Response(404, {"schema_version": 1, "error": {
    "code": "not_found", "message": "resource not found", "retryable": False}})


@contextlib.contextmanager
def no_legacy(reads):
    """Legacy-швы #459/#618 под отказом: канонический путь их не касается.

    reads помнит любое чтение клиентской политики: канонический add её не
    читает вовсе, а update/recycle -- тем более (их admission не связан)."""
    def refuse(*a, **k):
        raise AssertionError("legacy orchestration reached on the canonical path")
    with patched(ops, add=refuse, update=refuse), \
            patched(puppets, recycle=refuse), \
            patched(context, here=refuse), \
            patched(_common, workspace_text=lambda o: reads.append(o) or
                    (PLAY, {"source": "origin", "present": True, "tasks": 1})):
        yield


def probe(argv, *responses, words="add"):
    """Командлет на каноническом шве -> (fixture, out, err, code, reads).

    Транспорт -- TLS-двойник `_control_fixture` (тот же, что у
    control_consumers): HTTP-обмен настоящий, сервер -- опубликованный
    конверт. Context -- transient `_control.use`, как у MCP/tests."""
    fx = Fixture(*responses)
    reads = []
    with patched(api, _DeadlineHTTPSConnection=fx.factory), \
            _control.use(api.ServerContext(API, TOKEN)), no_legacy(reads):
        out, err, code = run_command(cli.main, [words, *argv])
    return fx, out, err, code, reads


def body_of(fx):
    """Тело последнего POST -- то, что фронтенд положил на провод."""
    return json.loads(fx.calls[-1][2])


def posts(fx):
    return [call for call in fx.calls if call[0] == "POST"]


def succeeded(rid=KEY):
    """Опубликованный успешный receipt с указанным ключом запроса."""
    value = create_operation("succeeded")
    value["operation"]["request_id"] = rid
    return value


def main():
    c = Checks()

    # ── 1. Роль на проводе: definition_id, request_id, без политики ─────
    # HYPOTHESIS: клиент везёт workspace и не несёт роли.
    # SOLUTION: канонический add несёт approved immutable definition_id,
    # request_id и НИ ОДНОГО поля клиентской политики; политика -- на сервере.
    fx, out, err, code, reads = probe(CREATE, Response(202, succeeded()))
    add = body_of(fx)
    c.check("#618 add carries the role definition, a request_id and no client policy",
            add["params"] == {"node_id": NODE, "definition_id": DEFINITION}
            and add["request_id"] == KEY
            and "workspace" not in add and "bootstrap_sent" not in add,
            add)
    c.check("#618 add reads no client policy", reads == [], reads)
    c.check("#618 one registration creates one target",
            [call[0] for call in fx.calls] == ["POST"]
            and fx.calls[0][1] == "/api/v1/projects/" + PROJECT + "/commands/create-puppet",
            fx.calls)
    c.check("#618 the commandlet succeeds on a succeeded receipt", code == 0, (code, err))
    c.check("#618 the success names the created puppet",
            PUPPET in json.loads(out)["operation"]["target"]["id"], out)
    c.check("#618 the success is silent in stderr", err == "", err)

    # ── 1b. Та же тройка для актора-гранта: провод не различает актора ──
    # SOLUTION: клиентская сторона роли не изобретает -- definition_id
    # один и тот же; кого пускать, решает сервер по identity токена.
    with patched_env(MOP_SOUL_DIR="/role-bundle", MOP_PUPPET="pu-alpha-1"):
        fx, out, err, code, reads = probe(CREATE, Response(202, succeeded()))
    add = body_of(fx)
    c.check("#618 the granted actor sends the same role definition and no policy",
            add["params"]["definition_id"] == DEFINITION
            and add["request_id"] == KEY and "workspace" not in add
            and code == 0 and reads == [], (add, code, reads))

    # ── 2. Роль обязательна: отказ ДО первого вызова ─────────────────────
    # HYPOTHESIS: add без роли уехал бы на сервер (или завёл бы безролевого).
    # SOLUTION: definition_id обязателен в разборе до context/TLS I/O.
    fx, out, err, code, reads = probe(
        [PROJECT, "--node-id", NODE, "--epoch", EPOCH], Response(202, succeeded()))
    c.check("#618 add without a role definition refuses before any call",
            fx.calls == [] and code not in (0, None)
            and "invalid command arguments" in err, (code, err, fx.calls))

    # ── 2b. Неизвестность регистрации: unknown с request_id, без повтора ─
    # HYPOTHESIS: ответ регистрации не пришёл -- исход неизвестен.
    # SOLUTION: POST без ответа -> GET-восстановление по тому же ключу;
    # ровно один POST, отказ называет request_id и "unknown".
    fx, out, err, code, reads = probe(
        CREATE, OSError("private lost reply"), NOT_FOUND)
    c.check("#618 an unanswered registration names the request_id and creates nothing blindly",
            code == 3 and len(posts(fx)) == 1 and out == ""
            and KEY in err and "unknown" in err, (code, out, err, fx.calls))

    # ── 3. Легаси --role: ранний отказ, политика не читается ─────────────
    # HYPOTHESIS (#662): add без роли у клиента -- прежний вызов с политикой.
    # SOLUTION: роли-имени и клиентской политики на каноническом проводе нет;
    # легаси-флаг отвергается разбором до I/O, чтения политики не происходит.
    fx, out, err, code, reads = probe(CREATE + ["--role", "master"],
                                      Response(202, succeeded()))
    c.check("#662 a legacy --role add refuses before any call and reads no policy",
            fx.calls == [] and code not in (0, None)
            and reads == [] and "invalid command arguments" in err,
            (code, err, fx.calls, reads))

    # ── 4. Старый сервер (без create-puppet) -- громкий отказ, без повтора ─
    # HYPOTHESIS: роль уехала бы в никуда на сервере без ролей.
    # SOLUTION: сервер без admission отвечает not_found; командлет отказывает,
    # второго POST нет.
    fx, out, err, code, reads = probe(
        CREATE, Response(404, {"schema_version": 1, "error": {
            "code": "not_found", "message": "unsupported resource",
            "retryable": False}}))
    c.check("#618 add against a server without the role admission refuses",
            code == 1 and out == "" and len(posts(fx)) == 1
            and "not_found" in err, (code, out, err, fx.calls))

    # ── 5. Запрещённая роль цели: отказ сервера доезжает как причина ─────
    # HYPOTHESIS: allowlist проверяет сервер; SOLUTION: фронтенд не дублирует
    # список -- причина отказа сервера едет в stderr, эхо её не маскирует.
    fx, out, err, code, reads = probe(
        CREATE, Response(403, {"schema_version": 1, "error": {
            "code": "permission_denied",
            "message": "role definition is not allowed to spawn this role",
            "retryable": False}}))
    c.check("#618 a forbidden target role is refused by the server",
            body_of(fx)["params"]["definition_id"] == DEFINITION
            and len(posts(fx)) == 1, (body_of(fx), fx.calls))
    c.check("#618 the refusal surfaces as the commandlet's failure",
            code == 1 and out == "", (code, out))
    c.check("#618 the server's refusal reason reaches stderr, echo does not mask it",
            "spawn" in err and "permission_denied" in err, err)

    # ── 5b. Неполный receipt: не успех, второго создания нет ─────────────
    # HYPOTHESIS: сервер ответил 202, но конверт не держит target binding --
    # это не успех и не имя созданного, а неизвестность исхода.
    # SOLUTION: неполный receipt -> ProtocolError -> unknown по request_id;
    # второго POST нет.
    bad = create_operation("accepted")
    bad["operation"]["target"] = {"id": PUPPET, "kind": "puppet", "revision": "1"}
    fx, out, err, code, reads = probe(CREATE, Response(202, bad), NOT_FOUND)
    c.check("#618 a receipt without the target binding fails as unknown",
            code == 3 and KEY in err and "unknown" in err
            and [call[0] for call in fx.calls] == ["POST", "GET"],
            (code, err, fx.calls))
    c.check("#618 no second creation follows a bad receipt",
            len(posts(fx)) == 1, fx.calls)

    # ── 5d. Нестрогий state: подтверждение -- только конечный state ──────
    # HYPOTHESIS: truthy-подобный state обходил бы валидацию.
    # SOLUTION: state -- строка из конечного множества; None/False/1/"yes"
    # отвергаются как неизвестность, без второго POST.
    for lie in (None, False, 1, "yes"):
        bad = create_operation("accepted")
        bad["operation"]["state"] = lie
        fx, out, err, code, reads = probe(CREATE, Response(202, bad), NOT_FOUND)
        c.check(f"#618 a non-canonical state ({lie!r}) is no role confirmation",
                code != 0 and len(posts(fx)) == 1 and out == ""
                and KEY in err and "unknown" in err,
                (lie, code, out, err, fx.calls))

    # ── 5c. Обрыв ПОСЛЕ приёма: имя admitted-операции + первопричина ─────
    # HYPOTHESIS: alloc/wait молчит, когда команда УЖЕ принята.
    # SOLUTION: это не эхо и не неизвестность: отказ называет admitted
    # operation и причину; второго POST нет.
    fx, out, err, code, reads = probe(
        CREATE + ["--wait", "1"], Response(202, create_operation("accepted")),
        OSError("private low-level detail"))
    c.check("#618 a post-registration outage names the admitted operation and its cause",
            code == 3 and len(posts(fx)) == 1 and OP in err
            and "transport unavailable" in err and "unknown" not in err
            and "private low-level detail" not in err, (code, err, fx.calls))

    # ── 6. MCP-объявление add: роль и восстановление ─────────────────────
    # HYPOTHESIS: MCP add не знает роли; SOLUTION: аргумент definition_id,
    # действие operations.view_operation вместо легаси ping/кластерных глаголов.
    decl = cli_add.MCP
    args = {a["name"]: a for a in decl.get("args", [])}
    c.check("#618 the MCP add declares the role definition argument",
            args.get("definition_id", {}).get("flag") == "--definition-id", decl)
    c.check("#618 the MCP add keeps recovery in its action catalogue",
            "operations.view_operation" in decl.get("actions", [])
            and "cluster_verbs" not in decl and "node_verbs" not in decl
            and decl.get("human_only") is True, decl)

    # ── 7. Update: admission не связан -- явный unsupported, без политики ─
    # HYPOTHESIS: update командлет прочитал бы политику рабочей копии.
    # SOLUTION (#793): update admission в этом релизе не связан; отказ ДО
    # сети и ДО чтения политики -- один и тот же для человека и гранта.
    fx, out, err, code, reads = probe(["pu-alpha-1"], words="update")
    c.check("#618 update refuses explicitly instead of reading a client policy",
            code == 2 and out == "" and reads == [] and fx.calls == [],
            (code, out, err, reads, fx.calls))
    with patched_env(MOP_SOUL_DIR="/role-bundle", MOP_PUPPET="pu-alpha-1"):
        fx2, out2, err2, code2, reads2 = probe(["pu-alpha-1"], words="update")
    c.check("#618 the granted update has no private channel -- the same canonical refusal",
            (fx2.calls, out2, err2, code2, reads2) == ([], out, err, code, reads),
            (code2, err2, fx2.calls, reads2))
    c.check("#618 the update refusal names the missing admission",
            err == "unsupported: update admission is not bound in this release\n",
            err)

    # ── 8. Recycle: admission не связан -- отказ ДО любого разрушения ────
    # HYPOTHESIS: recycle всегда собирал workspace_of и сносил джоб.
    # SOLUTION (#793): admission не связан, ни чтения, ни delete/update/wipe.
    fx, out, err, code, reads = probe(["pu-alpha-1"], words="recycle")
    c.check("#618 recycle refuses explicitly instead of reading a client policy",
            code == 2 and out == "" and reads == [] and fx.calls == [],
            (code, out, err, reads, fx.calls))
    c.check("#618 the recycle refusal names the missing admission",
            err == "unsupported: recycle admission is not bound in this release\n",
            err)

    # ── 10. --request-id: ручной повтор тем же идентификатором ──────────
    rid = str(uuid.uuid4())
    fx, out, err, code, reads = probe(
        [PROJECT, "--node-id", NODE, "--definition-id", DEFINITION,
         "--epoch", EPOCH, "--request-id", rid, "--not-after",
         "2026-10-16T10:05:00Z"], Response(202, succeeded(rid)))
    c.check("#618 --request-id rides the registration as given",
            body_of(fx)["request_id"] == rid and code == 0
            and PUPPET in out, (body_of(fx), code, out))

    # 10b. Ретрай тем же id после неизвестности: GET сводит к admitted
    # receipt того же ключа, второго POST нет.
    fx, out, err, code, reads = probe(
        [PROJECT, "--node-id", NODE, "--definition-id", DEFINITION,
         "--epoch", EPOCH, "--request-id", rid, "--not-after",
         "2026-10-16T10:05:00Z"], OSError("private lost reply"),
        Response(200, succeeded(rid)))
    c.check("#618 a retry with the same request_id reconciles to the same operation",
            code == 0 and len(posts(fx)) == 1
            and [call[0] for call in fx.calls] == ["POST", "GET"]
            and ("request_id=" + rid) in fx.calls[-1][1]
            and PUPPET in out, (code, out, err, fx.calls))

    # 10c. Тот же id с ДРУГОЙ полезной нагрузкой -- отказ сервера до эффекта.
    fx, out, err, code, reads = probe(
        [PROJECT, "--node-id", NODE, "--definition-id", "b5600b0d-15cc-4fa3-ab0e-7c59d7e98939",
         "--epoch", EPOCH, "--request-id", rid, "--not-after",
         "2026-10-16T10:05:00Z"], Response(409, {"schema_version": 1, "error": {
             "code": "request_conflict",
             "message": "request key already used with a different payload",
             "retryable": False}}))
    c.check("#618 a changed payload under the same request_id refuses",
            code != 0 and out == "" and "request" in err
            and len(posts(fx)) == 1, (code, out, err, fx.calls))

    # 10d. Мусорный id -- ранний отказ до первого вызова.
    fx, out, err, code, reads = probe(
        [PROJECT, "--node-id", NODE, "--definition-id", DEFINITION,
         "--epoch", EPOCH, "--request-id", "nope", "--not-after",
         "2026-10-16T10:05:00Z"], Response(202, succeeded()))
    c.check("#618 a malformed request_id refuses before any call",
            fx.calls == [] and code not in (0, None)
            and "invalid control client input" in err, (code, err, fx.calls))

    # 10e. id без роли -- ранний usage, на провод ничего.
    fx, out, err, code, reads = probe(
        [PROJECT, "--node-id", NODE, "--epoch", EPOCH, "--request-id", rid,
         "--not-after", "2026-10-16T10:05:00Z"], Response(202, succeeded()))
    c.check("#618 a request_id without a role definition is an early usage refusal",
            fx.calls == [] and code not in (0, None)
            and "invalid command arguments" in err, (code, err, fx.calls))

    # 10f. Без флага -- свежий canonical UUID, как прежде.
    fresh = []
    original_id = api.new_request_id

    def recording_id(value=None):
        result = original_id(value)
        fresh.append(result)
        return result

    class Late(Response):
        """Receipt, собранный при чтении: свежий ключ известен только после
        разбора argv, а конверт обязан нести именно его."""

        def __init__(self, *a, **k):
            Response.__init__(self, *a, **k)
            self.body, self.built = b"", False

        def read1(self, size):
            if not self.built:
                self.body, self.built = json.dumps(succeeded(fresh[0])).encode(), True
            return Response.read1(self, size)

    with patched(api, new_request_id=recording_id):
        fx, out, err, code, reads = probe(
            [PROJECT, "--node-id", NODE, "--definition-id", DEFINITION,
             "--epoch", EPOCH, "--not-after", "2026-10-16T10:05:00Z"],
            Late())
    got_id = body_of(fx)["request_id"]
    c.check("#618 without the flag the request_id is a fresh UUID",
            isinstance(got_id, str) and str(uuid.UUID(got_id)) == got_id
            and got_id != rid, got_id)

    # 10g. MCP-объявление: аргумент request_id.
    c.check("#618 the MCP add declares the request_id argument",
            args.get("request_id", {}).get("flag") == "--request-id", decl)

    # ── 9. Recycle гранта: неполный маршрут -- отказ ДО сноса ────────────
    # HYPOTHESIS: рецикл сносит джоб до перерегистрации; урезанная роль
    # теряет пул на полпути. SOLUTION (#793): admission не связан, и отказа
    # ДО любого разрушительного глагола достаточно: ни HTTP, ни wipe.
    # 9a. Грант с бандлом: отказ до delete/update/stop/restart, wipe нет.
    with patched_env(MOP_SOUL_DIR="/role-bundle", MOP_PUPPET="pu-alpha-1"):
        fx, out, err, code, reads = probe(["pu-alpha-1"], words="recycle")
    destructive = [call for call in fx.calls
                   if call[1].rsplit("/", 1)[-1] in ("delete", "restart", "stop-job", "update")]
    c.check("#618 a granted recycle refuses before any damage",
            code == 2 and destructive == [] and fx.calls == [] and reads == [],
            (code, destructive, fx.calls, err, reads))

    # 9b. Бандл недоступен -- тот же отказ, не молчание.
    with patched_env(MOP_SOUL_DIR=None, MOP_PUPPET=None):
        fx, out, err, code, reads = probe(["pu-alpha-1"], words="recycle")
    c.check("#618 an unavailable bundle keeps the same recycle refusal",
            code == 2 and fx.calls == [] and out == ""
            and err == "unsupported: recycle admission is not bound in this release\n",
            (code, out, err, fx.calls))

    # 9c. Человек и грант: один канонический отказ, рабочая копия не читается.
    fx, out, err, code, reads = probe(["pu-alpha-1"], words="recycle")
    c.check("#618 the human and granted recycle share one refusal with no policy read",
            code == 2 and reads == [] and fx.calls == []
            and err == "unsupported: recycle admission is not bound in this release\n",
            (code, err, reads, fx.calls))

    return c.report("roleops")


if __name__ == "__main__":
    sys.exit(main())
