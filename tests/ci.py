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
    """`mop dev ci` — группа с глаголами; MCP-экспозиции у dev нет (#515)."""
    from mop import cli
    verbs = set((cli.verbs().get("dev") or {}).get("ci") or {})   # дерево (#253): глаголы -- ключи
    want = {"list", "show", "log", "why", "lint", "retry", "cancel", "runners"}
    if not c.check("mop dev ci verbs", not (verbs != want),
                   f"{sorted(verbs or ())}, wanted {sorted(want)}"):
        return
    # #515: группа dev закрыта в MCP целиком -- ни один лист не объявлен,
    # включая cancel; трекером и CI мастер пользуется из шелла (#450).
    for verb in sorted(want):
        decl = cli.declared(os.path.join(cli.PACKAGE, "dev", "ci", f"{verb}.py"))
        c.check(f"mop dev ci {verb} carries no MCP declaration (#515)",
                decl is None, repr(decl))
    c.check("mop dev ci itself carries no MCP declaration (#515)",
            cli.declared(os.path.join(cli.PACKAGE, "dev", "ci", "__init__.py")) is None)
    # Без глагола — список, с числом — пайплайн: разбирает сама группа.
    route = getattr(__import__("mop.cli.dev.ci", fromlist=["route"]), "route", None)
    if not c.check("mop.cli.dev.ci has route", route is not None):
        return
    for argv, want_route in (([], ("list", [])), (["42"], ("show", ["42"])),
                             (["42", "--failed"], ("show", ["42", "--failed"])),
                             (["nope"], None)):
        c.expect(f"route({argv})", route(argv), want_route)


def check_pipeline_yaml(c):
    """#678: один обязательный Python 3.12 / PostgreSQL 17 job с web gate.

    HYPOTHESIS: прежние две комбинации и отдельный web job не соответствуют
    новому контракту; ранний Python failure не должен скрывать web-проверку,
    а ранний отказ web-гейта -- остальные frontend-гейты.
    RED: прежний YAML нарушал 10 проверок; второй test runtime -- ещё 5;
    CHECK с одним build --check не запускал types и полный Vitest (fake mop
    logged only python+web-build).
    SOLUTION: один Python 3.12 container, PG17 и read-only Node toolchain;
    CHECK -- три frontend-гейта, каждый со своим кодом, без fail-fast.
    RESULT: все четыре команды выполняются при шести сочетаниях exit codes;
    actual --check client --web прошёл build-гейт, а types и полный Vitest
    выполнились и честно покрасили fixture своими отказами (см. #678).
    STATUS: FIXED — see #678
    """
    import json
    from pathlib import Path
    import subprocess
    import tempfile
    import yaml

    path = Path(__file__).resolve().parents[1] / '.gitlab-ci.yml'
    ci = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    jobs = {name: value for name, value in ci.items() if not name.startswith('.')
            and isinstance(value, dict) and ('script' in value or 'extends' in value)}
    c.expect('#678 one test job and the separately guarded deployment', sorted(jobs),
             ['deploy:mop', 'test:py3.12'])
    template = ci['.tests']
    variables = {**template.get('variables', {}), **ci['test:py3.12'].get('variables', {})}
    c.expect('#678 one test stage', template.get('stage'), 'test')
    c.expect('#678 strict mode is mandatory', variables.get('MOP_TESTS_STRICT'), '1')
    c.expect('#678 the complete suite is retained', variables.get('TESTS'), 'bin/mop dev test -v')
    c.expect('#678 only Python 3.12', variables.get('PYTHON_IMAGE'), 'python:3.12-slim-trixie')
    c.expect('#678 only PostgreSQL 17', variables.get('POSTGRES_IMAGE'), 'postgres:17-bookworm')
    c.expect('#678 matching PostgreSQL clients', variables.get('POSTGRES_MAJOR'), '17')
    c.expect('#678 declared Node toolchain', variables.get('NODE_IMAGE'), 'node:22-bookworm')
    check = variables.get('CHECK')
    check_script = check.get('value') if isinstance(check, dict) else check
    c.check('#678 dashboard verification remains mandatory',
            isinstance(check_script, str) and 'bin/mop dev web build --check' in check_script)
    c.check('#678 full frontend type checking remains mandatory',
            isinstance(check_script, str) and 'bin/mop dev web test --types' in check_script)
    c.check('#678 every frontend interaction test is selected',
            isinstance(check_script, str) and any(line.strip().startswith('bin/mop dev web test ||')
                                                for line in check_script.splitlines()))
    c.expect('#678 deploy waits for the combined gate', ci['deploy:mop'].get('needs'), ['test:py3.12'])
    c.expect('#678 deploy still requires the default branch and explicit target',
             ci['deploy:mop'].get('rules'),
             [{'if': '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH && $MOP_DEPLOY_TARGET'}])
    # #674: root-only UID gate живёт в объявленном SETUP того же единственного
    # test job -- после создания пользователей, до exec su ci, и только там:
    # внутри test-контейнера нет docker-сокета, TESTS остаётся чистым
    # непривилегированным suite.
    setup = template['variables']['SETUP']['value']
    c.check('#674 the root uid gate is declared in the owned setup',
            'python3 tests/_credential_uid_fixture.py' in setup)
    gate_builder = setup.index('python3 tests/_credential_uid_fixture.py') \
        if 'python3 tests/_credential_uid_fixture.py' in setup else -1
    c.check('#674 the gate runs after users exist and before the unprivileged suite',
            gate_builder >= 0 and setup.index('useradd') < gate_builder < setup.index('exec su ci'))
    c.check('#674 the unprivileged suite has no root gate and no docker need',
            '--cross-uid' not in (variables.get('TESTS') or ''))
    script = template['script'][-1]
    with tempfile.TemporaryDirectory(prefix='mop-ci-contract-') as directory:
        root = Path(directory)
        binary = root / 'docker'
        binary.write_text('#!' + sys.executable + '\n' + '''import json, os, subprocess, sys
if os.environ['PYTHON_IMAGE'] not in sys.argv:
    with open(os.environ['CALL_LOG'], 'a') as stream:
        stream.write(json.dumps('wrong-image') + '\\n')
    sys.exit(91)
env = dict(os.environ)
for index, value in enumerate(sys.argv[1:], 1):
    if value == '-e' and '=' in sys.argv[index + 1]:
        key, item = sys.argv[index + 1].split('=', 1)
        env[key] = item
sys.exit(subprocess.run(['bash', '-euc', env['TESTS']], cwd=env['CI_PROJECT_DIR'], env=env).returncode)
''')
        binary.chmod(0o700)
        (root / 'bin').mkdir()
        command = root / 'bin' / 'mop'
        command.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
cases = {
    ('dev', 'test', '-v'): ('python', 'PYTHON_RESULT'),
    ('dev', 'web', 'build', '--check'): ('web-build', 'WEB_BUILD_RESULT'),
    ('dev', 'web', 'test', '--types'): ('web-types', 'WEB_TYPES_RESULT'),
    ('dev', 'web', 'test'): ('web-tests', 'WEB_TEST_RESULT'),
}
kind, result = cases.get(tuple(sys.argv[1:]), ('other', None))
with open(os.environ['CALL_LOG'], 'a') as stream:
    stream.write(json.dumps(kind) + '\\n')
sys.exit(int(os.environ[result]) if result else 92)
''')
        command.chmod(0o700)
        log = root / 'calls.jsonl'
        setup_log = root / 'gate-calls.jsonl'
        # #674: gate живёт в SETUP, не в TESTS -- у окружения job нет
        # docker-сокета, и runner-контейнер обязан звать только mop suite.
        gate = root / 'gate.sh'
        gate.write_text('#!/bin/bash\n'
                        'python3 - <<\'PY\'\n'
                        'import json, os\n'
                        'with open(os.environ["CALL_LOG"], "a") as stream:\n'
                        '    stream.write(json.dumps("uid-gate") + "\\n")\n'
                        'PY\n'
                        'exit "$GATE_RESULT"\n')
        gate.chmod(0o700)
        for codes in ((0, 0, 0, 0, 0), (7, 0, 0, 0, 0), (3, 0, 0, 0, 0), (0, 9, 0, 0, 0),
                      (0, 0, 11, 0, 0), (0, 0, 0, 13, 0), (7, 9, 11, 13, 3)):
            gate_result, python_result, build_result, types_result, tests_result = codes
            log.write_text('')
            setup_log.write_text('')
            env = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ.get('PATH', ''),
                'CI_PROJECT_DIR': str(root), 'CI_JOB_ID': '678-fixture',
                'PYTHON_IMAGE': str(variables['PYTHON_IMAGE']), 'NODE_IMAGE': str(variables.get('NODE_IMAGE', 'node:22-bookworm')),
                'GATE_RESULT': str(gate_result), 'PYTHON_RESULT': str(python_result),
                'WEB_BUILD_RESULT': str(build_result),
                'WEB_TYPES_RESULT': str(types_result), 'WEB_TEST_RESULT': str(tests_result), 'CALL_LOG': str(log),
                'TESTS': variables.get('TESTS') or 'bin/mop dev test -v', 'CHECK': check_script or '', 'SETUP': 'unused'}
            # SETUP исполняет gate и затем TESTS: отказ gate красит прогон
            # до suite, отказ suite красит прогон после gate. Отдельный лог
            # gate-прогона: line-протокол общий, но прогон отдельный.
            setup_result = subprocess.run(['bash', '-euc',
                f"'{gate}'"], env={**env, 'CALL_LOG': str(setup_log)},
                text=True, capture_output=True)
            result = subprocess.run(['bash', '-euc', script], env=env, text=True, capture_output=True)
            entries = ([json.loads(line) for line in setup_log.read_text().splitlines()]
                       + [json.loads(line) for line in log.read_text().splitlines()])
            c.expect(f'#674 gate and every check run for exit codes {codes}',
                     entries, ['uid-gate', 'python', 'web-build', 'web-types', 'web-tests'])
            c.expect(f'#674 any failed gate or check rejects the job {codes}',
                     result.returncode == 0 and setup_result.returncode == 0,
                     all(code == 0 for code in codes))


def main():
    c = Checks()
    check_cases(c)
    try:
        check_group(c)
    except Exception as e:  # noqa: BLE001
        c.fail("mop dev ci group", f"raised {type(e).__name__}: {e}")
    try:
        check_pipeline_yaml(c)
    except Exception as e:  # noqa: BLE001
        c.fail("#525 pipeline yaml check", f"raised {type(e).__name__}: {e}")
    return c.report("ci")


if __name__ == "__main__":
    sys.exit(main())
