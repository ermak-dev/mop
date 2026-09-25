#!/usr/bin/env python3
"""Чистая логика `mop dev ci` без GitLab: python3 tests/ci.py

#233: пайплайны mop покраснели (#230), и увидеть почему можно было только
разовым скриптом к API (OAuth-токен, /projects/39/jobs/<id>/trace) — ровно
тот обход снаружи, который CLAUDE.md запрещает. Порт `bin/ci` из rugent:
библиотека в mop/common/gitlab.py, командлеты — группа mop/cli/dev/ci.

HYPOTHESIS: у mop нет ни одного глагола про пайплайны, кроме pipeline(sha)
для deploy (#231), и отказы/строки, которые решают, что показать модели,
некуда проверить без сети.
SOLUTION: чистые помощники в mop/common/gitlab.py (mark, duration, tail, screen_lines,
is_failure,
retry_blocked, pipeline_retry_blocked, pipeline_cancel_blocked,
pipeline_failure, pipeline_line, job_line, runner_line, running_line,
lint_report, why_lines) и командлеты поверх них.
RESULT: проверки ниже. STATUS: FIXED — see #233
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import gitlab  # noqa: E402

URL = "https://git.example.dev/group/proj/-/pipelines/42"
JOB_URL = "https://git.example.dev/group/proj/-/jobs/7"


def job(**kw):
    base = {"id": 7, "status": "success", "stage": "test", "name": "unit",
            "duration": 62.4, "allow_failure": False, "web_url": JOB_URL}
    return {**base, **kw}


def pipe(**kw):
    base = {"id": 42, "status": "failed", "sha": "cff9f6812345678901234567890abcdef0123456",
            "ref": "master", "created_at": "2026-09-24T10:11:12.345Z", "web_url": URL}
    return {**base, **kw}


def check_cases(c):
    """Каждый случай -- c.expect(что, получено, ожидалось)."""
    g = gitlab

    def fn(name):
        f = getattr(g, name, None)
        if f is None:
            raise AttributeError(f"no gitlab.{name}")
        return f

    def add(what, thunk, want):
        try:
            got = thunk()
        except Exception as e:  # noqa: BLE001 -- отказ тоже ответ проверки
            got = f"raised {type(e).__name__}: {e}"
        c.expect(what, got, want)

    # Метка статуса — слово, а не значок: вывод читает модель через MCP.
    # Провал пайплайна — капсом, он и есть то, что ищут глазами в списке.
    for status, want in (("success", "ok"), ("failed", "FAILED"), ("running", "running"),
                         ("canceled", "canceled"), (None, "?")):
        add(f"mark({status!r})", lambda s=status: fn("mark")(s), want)

    # Длительность: у ещё не стартовавшей джобы duration — null.
    for secs, want in ((None, "-"), (0, "0s"), (59.9, "59s"), (60, "1m00s"),
                       (62.4, "1m02s"), (3725, "62m05s")):
        add(f"duration({secs!r})", lambda s=secs: fn("duration")(s), want)

    # Хвост трассы: последние n строк, пустой конец трассы не считается
    # строкой — иначе «последние 60» теряли бы одну на перевод строки.
    add("tail 2", lambda: fn("tail")("a\nb\nc\n", 2), "b\nc")
    add("tail more than there is", lambda: fn("tail")("a\nb", 60), "a\nb")
    add("tail None is full", lambda: fn("tail")("a\nb\nc", None), "a\nb\nc")
    add("tail empty", lambda: fn("tail")("", 5), "")
    add("tail None text", lambda: fn("tail")(None, 5), "")

    # Трасса GitLab — вывод для терминала: строка секции — это
    # «section_start:<t>:<имя>\r\x1b[0K<текст>», и терминал показывает только
    # то, что после последнего \r. Без этого хвост читается как мусор маркеров.
    add("screen_lines section", lambda: fn("screen_lines")(
        "section_start:1695:step_script\r\x1b[0KExecuting step\nok\r\n"),
        "\x1b[0KExecuting step\nok\n")
    add("screen_lines progress", lambda: fn("screen_lines")("10%\r50%\r100%\ndone"),
        "100%\ndone")
    add("screen_lines None", lambda: fn("screen_lines")(None), "")

    # Провал — failed и не allow_failure: терпимый провал пайплайн не валит,
    # и «почему красный» не должен показывать его причиной.
    add("is_failure failed", lambda: fn("is_failure")(job(status="failed")), True)
    add("is_failure allowed", lambda: fn("is_failure")(
        job(status="failed", allow_failure=True)), False)
    add("is_failure success", lambda: fn("is_failure")(job()), False)

    # Повтор идущего: GitLab ответил бы 403 без объяснения — отказ с состоянием.
    for status in ("running", "pending", "created"):
        add(f"retry_blocked {status}", lambda s=status: fn("retry_blocked")(job(status=s)),
            f"job 7 is {status}: wait for it to finish before retrying")
        add(f"pipeline_retry_blocked {status}",
            lambda s=status: fn("pipeline_retry_blocked")(pipe(status=s)),
            f"pipeline 42 is {status}: wait for it to finish before retrying")
    for status in ("failed", "canceled", "success"):
        add(f"retry_blocked {status}", lambda s=status: fn("retry_blocked")(job(status=s)), None)
        add(f"pipeline_retry_blocked {status}",
            lambda s=status: fn("pipeline_retry_blocked")(pipe(status=s)), None)

    # Отмена законченного — не ошибка GitLab, а тихое ничего: отказ с состоянием.
    for status in ("success", "failed", "canceled", "skipped"):
        add(f"pipeline_cancel_blocked {status}",
            lambda s=status: fn("pipeline_cancel_blocked")(pipe(status=s)),
            f"pipeline 42 is {status}: nothing to cancel")
    for status in ("running", "pending", "created"):
        add(f"pipeline_cancel_blocked {status}",
            lambda s=status: fn("pipeline_cancel_blocked")(pipe(status=s)), None)

    # Отвергнутый .gitlab-ci.yml валит пайплайн без единой джобы: причина —
    # в самом пайплайне, и без неё «почему» отвечало бы пустотой (#230).
    add("pipeline_failure yaml", lambda: fn("pipeline_failure")(pipe(
        yaml_errors="jobs:unit config contains unknown keys: scrpt",
        detailed_status={"text": "failed"})),
        ["yaml_errors: jobs:unit config contains unknown keys: scrpt",
         "detailed_status: failed"])
    add("pipeline_failure reason", lambda: fn("pipeline_failure")(pipe(
        failure_reason="config_error")), ["failure_reason: config_error"])
    add("pipeline_failure none", lambda: fn("pipeline_failure")(pipe()), [])

    add("pipeline_line", lambda: fn("pipeline_line")(pipe()),
        ("FAILED", "42", "cff9f681", "master", "2026-09-24 10:11", URL))
    add("pipeline_line no created", lambda: fn("pipeline_line")(pipe(created_at=None)),
        ("FAILED", "42", "cff9f681", "master", "-", URL))

    add("job_line", lambda: fn("job_line")(job()),
        ("ok", "7", "test", "unit", "1m02s", ""))
    add("job_line failed", lambda: fn("job_line")(job(status="failed")),
        ("FAILED", "7", "test", "unit", "1m02s", ""))
    # Терпимый провал — не капсом: он пайплайн не валил.
    add("job_line allowed", lambda: fn("job_line")(job(status="failed", allow_failure=True)),
        ("failed", "7", "test", "unit", "1m02s", "(allow_failure)"))

    add("runner_line on", lambda: fn("runner_line")(
        {"id": 5, "description": "hyper", "active": True, "paused": False,
         "status": "online", "tag_list": ["docker", "pve"]}),
        ("ON", "5", "hyper", "online", "docker,pve"))
    add("runner_line paused", lambda: fn("runner_line")(
        {"id": 6, "description": "", "paused": True, "status": "offline", "tag_list": []}),
        ("OFF", "6", "-", "offline", "-"))

    add("running_line", lambda: fn("running_line")(
        job(status="running", ref="feat/233-mop-ci",
            runner={"id": 5, "description": "hyper"})),
        ("7", "test", "unit", "feat/233-mop-ci", "runner 5 hyper"))
    add("running_line no runner", lambda: fn("running_line")(
        job(status="running", ref="master", runner=None)),
        ("7", "test", "unit", "master", "no runner"))

    # lint: валидный — число и имена джоб; невалидный — ошибки и выход 1.
    add("lint_report valid", lambda: fn("lint_report")(
        {"valid": True, "errors": [], "warnings": ["jobs:x may run twice"],
         "jobs": [{"name": "unit"}, {"name": "hermetic"}]}),
        (["valid — 2 job(s): unit, hermetic", "warning: jobs:x may run twice"], True))
    add("lint_report invalid", lambda: fn("lint_report")(
        {"valid": False, "errors": ["jobs:unit script missing"], "warnings": []}),
        (["invalid", "error: jobs:unit script missing"], False))

    # why: провалившие — с хвостами; терпимые — названы и отделены; без
    # провалившихся джоб — причина самого пайплайна.
    failed = job(id=8, status="failed", name="hermetic", duration=5,
                 web_url="https://git.example.dev/group/proj/-/jobs/8")
    allowed = job(id=9, status="failed", name="lint", allow_failure=True, duration=None)
    add("why_lines jobs", lambda: fn("why_lines")(
        pipe(), [job(), failed, allowed], {8: "boom\nFAILED 1/3"}),
        [f"pipeline 42 failed: {URL}",
         "",
         "job 8 test/hermetic failed (5s): https://git.example.dev/group/proj/-/jobs/8",
         "boom",
         "FAILED 1/3",
         "",
         "job 9 test/lint failed (allow_failure): did NOT fail the pipeline"])
    add("why_lines yaml", lambda: fn("why_lines")(
        pipe(yaml_errors="bad include"), [], {}),
        [f"pipeline 42 failed: {URL}", "no jobs failed; the pipeline itself says:",
         "yaml_errors: bad include"])
    add("why_lines no reason", lambda: fn("why_lines")(pipe(), [], {}),
        [f"pipeline 42 failed: {URL}", "no jobs failed, and GitLab gives no reason"])
    add("why_lines green", lambda: fn("why_lines")(pipe(status="success"), [job()], {}),
        [f"pipeline 42 success: {URL}", "nothing failed"])


def check_group(c):
    """`mop dev ci` — группа с глаголами; MCP только у читающих (#233)."""
    from mop import cli
    verbs = set((cli.verbs().get("dev") or {}).get("ci") or {})   # дерево (#253): глаголы -- ключи
    want = {"list", "show", "log", "why", "lint", "retry", "cancel", "runners"}
    if not c.check("mop dev ci verbs", not (verbs != want),
                   f"{sorted(verbs or ())}, wanted {sorted(want)}"):
        return
    # #233 давал читающим глаголам MCP; #254 снимает его со всех: dev --
    # внутренние команды разработчика, мастер зовёт их из шелла, и
    # пространство целиком закрыто для инструментов (cli.PRIVATE).
    for verb in sorted(want):
        decl = cli.declared(os.path.join(cli.PACKAGE, "dev", "ci", f"{verb}.py"))
        c.check(f"mop dev ci {verb} must stay out of MCP", decl is None, repr(decl))
    c.check("mop dev ci itself must stay out of MCP: its verbs are the tools",
            not (cli.declared(os.path.join(cli.PACKAGE, "dev", "ci", "__init__.py")) is not None))
    # Без глагола — список, с числом — пайплайн: разбирает сама группа.
    route = getattr(__import__("mop.cli.dev.ci", fromlist=["route"]), "route", None)
    if not c.check("mop.cli.dev.ci has route", route is not None):
        return
    for argv, want_route in (([], ("list", [])), (["42"], ("show", ["42"])),
                             (["42", "--failed"], ("show", ["42", "--failed"])),
                             (["nope"], None)):
        c.expect(f"route({argv})", route(argv), want_route)


def main():
    c = Checks()
    check_cases(c)
    try:
        check_group(c)
    except Exception as e:  # noqa: BLE001
        c.fail("mop dev ci group", f"raised {type(e).__name__}: {e}")
    return c.report("ci")


if __name__ == "__main__":
    sys.exit(main())
