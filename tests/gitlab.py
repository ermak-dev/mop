#!/usr/bin/env python3
"""Чистая логика трекера без GitLab: python3 tests/gitlab.py

Переходы меток и имена веток — единственное в `mop/common/gitlab.py`, что можно
проверить без сети, и единственное, где ошибка молчит: лишняя метка одной
группы не отказ, GitLab оставит одну из двух и не скажет какую, а задача
выпадет из выборки, которой её ищут.
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import gitlab  # noqa: E402

ORIGINS = [
    ("git@git.example.dev:group/proj.git", ("git.example.dev", "group/proj")),
    ("ssh://git@git.example.dev:2222/group/sub/proj.git",
     ("git.example.dev", "group/sub/proj")),
    ("https://git.example.dev/group/proj.git", ("git.example.dev", "group/proj")),
    ("", ("", "")),
]

BRANCHES = [
    (["bug", "sev::high"], 12, None, "bug/12"),
    (["bug"], 12, "Папет залипает на диалоге", "bug/12"),
    (["bug"], 12, "clone holds work", "bug/12-clone-holds-work"),
    (["feature"], 7, "driver seam", "feat/7-driver-seam"),
    (["type::docs"], 3, "BUS", "docs/3-bus"),
    # Дефект перевешивает тип: задача не должна нести оба, но если несёт —
    # правда о работе в дефекте.
    (["bug", "feature"], 9, None, "bug/9"),
    ([], 1, None, "feat/1"),
]

STATUS = [
    (["status::live", "sev::low"], "wip", ["sev::low", "status::wip"]),
    (["status::wip"], "fixed", ["status::fixed"]),
    # Идемпотентность: повторный переход не кладёт вторую метку группы.
    (["status::wip"], "wip", ["status::wip"]),
    ([], "live", ["status::live"]),
]

# #202: смена статуса через relabel — решает ли она заодно состояние задачи.
# parked — «открыта, но не в работе»: закрытая #167, отложенная в бэклог,
# осталась закрытой и пропала из `mop dev bug list`, отложенное выглядело сделанным.
# relabel только открывает: закрытие — работа `mop dev bug close`, с комментарием.
# HYPOTHESIS: relabel открывает лишь статусы из OPEN_STATUSES, а parked стоял
# среди закрывающих. SOLUTION: parked переехал в OPEN_STATUSES, решение вынесено
# в gitlab.relabel_action. RESULT: parked на закрытой открывает, close больше
# не предлагает parked. STATUS: FIXED — see #202
RELABEL_ACTION = [
    ("closed", "parked", "reopen"),
    ("closed", "live", "reopen"),
    ("closed", "wip", "reopen"),
    ("closed", "fixed", "relabel"),
    ("closed", "noise", "relabel"),
    ("closed", "dup", "relabel"),
    ("closed", None, "relabel"),      # только --label: состояние не трогаем
    ("opened", "parked", "relabel"),
    ("opened", "fixed", "relabel"),   # закрывает close, не relabel
    ("opened", "live", "relabel"),
]


# #231: deploy катит HEAD рабочей копии сервера, а какой он — на совести того,
# кто последним сделал pull. 24.09 origin/master полчаса стоял красным на
# cff9f68 (регрессия #220), и deploy в это окно разнёс бы её по пулу.
# HYPOTHESIS: deploy не спрашивает у GitLab, зелёный ли катимый коммит.
# SOLUTION: gitlab.pipeline_verdict — статус пайплайна в «катить»/отказ;
# deploy.pipeline_refusals — настройка MOP_DEPLOY_NEEDS_GREEN (дефолт пуст —
# сегодняшнее поведение), громкий отказ без кредов GitLab, --skip-pipeline.
# RESULT: проверки ниже. STATUS: FIXED — see #231
SHA = "cff9f6812345678901234567890123456789abcd"
URL = "https://git.example.dev/group/proj/-/pipelines/42"
PIPELINE_VERDICT = [
    ({"status": "success", "web_url": URL}, None),
    ({"status": "failed", "web_url": URL}, f"pipeline failed for {SHA[:12]}: {URL}"),
    ({"status": "running", "web_url": URL}, f"wait for pipeline {URL} (running)"),
    ({"status": "pending", "web_url": URL}, f"wait for pipeline {URL} (pending)"),
    ({"status": "created", "web_url": URL}, f"wait for pipeline {URL} (created)"),
    (None, f"no pipeline for {SHA}"),
    # Неизвестное — отказ с именем: отменённый или пропущенный пайплайн не
    # говорит, что коммит зелёный.
    ({"status": "canceled", "web_url": URL},
     f"pipeline {URL} is canceled, not success: refusing"),
    ({"status": "skipped", "web_url": URL},
     f"pipeline {URL} is skipped, not success: refusing"),
]


def check_deploy_gate(c):
    """Настройка, отказ без кредов и флаг deploy (#231)."""
    from mop.common import config
    from mop.cli.server import deploy
    c.expect("MOP_DEPLOY_NEEDS_GREEN default ('' is no check)",
             config.SETTINGS.get("MOP_DEPLOY_NEEDS_GREEN", None), "")
    fn = getattr(deploy, "pipeline_refusals", None)
    if not c.check("mop server deploy has pipeline_refusals", fn is not None):
        return

    def boom(sha, ref=None):
        raise AssertionError("GitLab asked while the check is off")

    def green(sha, ref=None):
        return {"status": "success", "web_url": URL}

    def red(sha, ref=None):
        return {"status": "failed", "web_url": URL}

    def down(sha, ref=None):
        raise RuntimeError("GET /pipelines -> HTTP 502: bad gateway")

    def running(sha, ref=None):
        return {"id": 42, "status": "running", "web_url": URL}

    def jobs_down(pid):
        raise RuntimeError(f"GET /pipelines/{pid}/jobs -> HTTP 502")

    cases = [
        # Выключено — сегодняшнее поведение: GitLab не спрашивают вовсе.
        (("", False, SHA, boom), []),
        (("0", False, SHA, boom), []),
        # Включено без кредов — громкий отказ, а не молчаливый пропуск.
        (("1", False, SHA, boom),
         ["MOP_DEPLOY_NEEDS_GREEN=1 but no GitLab credentials in .env (GITLAB_TOKEN, or "
          "GITLAB_USER and GITLAB_PASSWORD): cannot check the pipeline of the commit "
          "being rolled out"]),
        (("1", True, SHA, green), []),
        (("1", True, SHA, red), [f"pipeline failed for {SHA[:12]}: {URL}"]),
        (("1", True, SHA, down),
         [f"cannot read the pipeline for {SHA}: GET /pipelines -> HTTP 502: bad gateway"]),
        # Рабочая копия без коммита: катимое не названо — и проверять нечего.
        (("1", True, "", green),
         ["cannot name the commit being rolled out: git rev-parse HEAD failed"]),
        # #239: идущий пайплайн — джобы спрашиваются, и зелёные тесты пропускают
        # идущий деплой; у законченного джобы не спрашивают вовсе.
        (("1", True, SHA, running, lambda pid: GREEN_TESTS), []),
        (("1", True, SHA, running,
          lambda pid: [ci_job(1, "test", "unit", "failed")] + GREEN_TESTS[2:]),
         [f"pipeline {URL} is running but job 1 test/unit failed: refusing"]),
        (("1", True, SHA, running, jobs_down),
         [f"cannot read the jobs of pipeline 42: GET /pipelines/42/jobs -> HTTP 502"]),
        (("1", True, SHA, green, boom), []),
        (("1", True, SHA, red, boom), [f"pipeline failed for {SHA[:12]}: {URL}"]),
        # Опечатка в .env не выключает проверку молча.
        (("yes", True, SHA, green),
         ["MOP_DEPLOY_NEEDS_GREEN='yes': want 1 (check) or empty (no check)"]),
    ]
    for args, want in cases:
        try:
            got = fn(*args)
        except (AssertionError, TypeError) as e:
            got = [f"raised: {e}"]
        c.expect(f"pipeline_refusals{args[:3]}", got, want)



# ── #335: пайплайн коммита -- на ветке раскатки ─────────────────────────
# HYPOTHESIS: 27.09 epic/314-dry создана на том же коммите, что master
# (5b92b44). deploy:mop пайплайна master отработал 6 с и сказал «5b92b440c952
# is still being tested; its own pipeline's deploy job rolls it out»:
# gitlab.pipeline(sha) берёт последний пайплайн коммита на любой ветке, и
# гейт увидел идущий пайплайн эпика, у которого джобы deploy нет. Сервер
# встал на 5b92b44, плейбук не прогнан, агенты остались на старом коде.
# SOLUTION: gitlab.pipeline(sha, ref=None) фильтрует по ветке, когда её
# назвали; гейт deploy спрашивает пайплайн ветки раскатки -- ветки origin
# по умолчанию; не названа -- отказ, а не любой ветки. STATUS: FIXED — see #335
def check_pipeline_by_ref_335(c):
    from mop.cli.server import deploy
    asked = []

    def call(method, path, payload=None, params=None):
        asked.append((method, path, params))
        return [{"id": 7, "status": "success", "web_url": URL}]
    old = {"sha": SHA, "order_by": "id", "sort": "desc", "per_page": 1}
    with patched(gitlab, call=call):
        try:
            gitlab.pipeline(SHA, ref="master")
        except TypeError as e:
            c.fail(f"#335 gitlab.pipeline takes ref: {e}")
        c.expect("#335 pipeline(sha, ref=...) sends ref in the query",
                 asked[-1:], [("GET", "/pipelines", {**old, "ref": "master"})])
        asked.clear()
        gitlab.pipeline(SHA)
        c.expect("#335 pipeline(sha) without ref keeps today's query",
                 asked, [("GET", "/pipelines", old)])

    fetched = []

    def green(sha, ref=None):
        fetched.append((sha, ref))
        return {"status": "success", "web_url": URL}
    try:
        got = deploy.pipeline_refusals("1", True, SHA, green, ref="master")
    except TypeError as e:
        got = [f"raised: {e}"]
    c.expect("#335 gate asks the pipeline of the rollout branch",
             (got, fetched), ([], [(SHA, "master")]))
    fetched.clear()
    try:
        got = deploy.pipeline_refusals("1", True, SHA, green, ref="")
    except TypeError as e:
        got = [f"raised: {e}"]
    c.expect("#335 gate with no nameable rollout branch refuses, asks nobody",
             (got, fetched),
             (["cannot name origin's default branch, the pipeline of the rollout branch "
               "is unknown -- run git remote set-head origin --auto"], []))
    try:
        got = deploy.pipeline_refusals("", True, SHA, green, ref="")
    except TypeError as e:
        got = [f"raised: {e}"]
    c.expect("#335 gate off: no branch needed, nothing asked", (got, fetched), ([], []))


# #239: CI катит mop сам — джоба deploy по ssh с forced command зовёт
# `mop server deploy --from-ci`. Пока она идёт, пайплайн running, и гейт #231
# отвечал «wait» самому деплою. Обход --skip-pipeline открыл бы украденному
# ключу красный master.
# HYPOTHESIS: гейт знает только статус пайплайна, не его джобы.
# SOLUTION: pipeline_verdict(p, sha, jobs): идущий пайплайн проходит, если
# каждая джоба вне стадии deploy success (терпимый провал не в счёт) и такие
# джобы есть; провал вне deploy — отказ с именем джобы; иначе — «wait».
# --from-ci: гейт обязателен, ветка по умолчанию, отслеживаемое не тронуто,
# fetch и --ff-only. STATUS: FIXED — see #239
def ci_job(jid, stage, name, status, allow=False):
    return {"id": jid, "stage": stage, "name": name, "status": status,
            "allow_failure": allow}


GREEN_TESTS = [ci_job(1, "test", "unit", "success"), ci_job(2, "test", "hermetic", "success"),
               ci_job(3, "deploy", "deploy", "running")]
RUNNING = {"status": "running", "web_url": URL}
VERDICT_JOBS = [
    # Тесты зелёные, идёт сам деплой — катить.
    (RUNNING, GREEN_TESTS, None),
    ({"status": "pending", "web_url": URL}, GREEN_TESTS, None),
    # Терпимый провал цвет не решает — и катить не мешает.
    (RUNNING, GREEN_TESTS + [ci_job(4, "lint", "ruff", "failed", allow=True)], None),
    # Тест ещё идёт — ждать, текст #231.
    (RUNNING, [ci_job(1, "test", "unit", "running"), ci_job(3, "deploy", "deploy", "running")],
     f"wait for pipeline {URL} (running)"),
    # Тест упал — отказ с именем джобы, а не «жди»: ждать тут нечего.
    (RUNNING, [ci_job(1, "test", "unit", "failed"), ci_job(3, "deploy", "deploy", "running")],
     f"pipeline {URL} is running but job 1 test/unit failed: refusing"),
    # Одни джобы deploy — тестов нет, пропускать не по чему.
    (RUNNING, [ci_job(3, "deploy", "deploy", "running")], f"wait for pipeline {URL} (running)"),
    (RUNNING, [], f"wait for pipeline {URL} (running)"),
    # Джобы не спрашивали — сегодняшнее поведение.
    (RUNNING, None, f"wait for pipeline {URL} (running)"),
    # Прочие статусы джобы не смотрят.
    ({"status": "failed", "web_url": URL}, GREEN_TESTS, f"pipeline failed for {SHA[:12]}: {URL}"),
    ({"status": "success", "web_url": URL}, [], None),
]

NEED = "--from-ci needs MOP_DEPLOY_NEEDS_GREEN=1 in .env: a CI rollout must pass the pipeline gate"
FROM_CI = [
    # (setting, skip, dry, branch, default, dirty) -> отказы
    (("1", False, False, "master", "master", []), []),
    (("", False, False, "master", "master", []), [NEED]),
    (("0", False, False, "master", "master", []), [NEED]),
    (("1", True, False, "master", "master", []),
     ["--from-ci and --skip-pipeline together: a CI rollout never skips the pipeline gate"]),
    (("1", False, True, "master", "master", []),
     ["--from-ci and --check together: --from-ci rolls out, --check is a dry run by hand"]),
    (("1", False, False, "feat/239-deploy-from-ci", "master", []),
     ["--from-ci: the working copy is on feat/239-deploy-from-ci, not master"]),
    (("1", False, False, "HEAD", "master", []),
     ["--from-ci: the working copy is on HEAD, not master"]),
    (("1", False, False, "master", "", []),
     ["--from-ci: cannot name origin's default branch -- run git remote set-head origin --auto"]),
    (("1", False, False, "master", "master", [" M mop/common/config.py", "M  deploy/site.yml"]),
     ["--from-ci: tracked files are modified: mop/common/config.py, deploy/site.yml"]),
]


# Под --from-ci «жди» значит одно: HEAD ушёл дальше коммита, запустившего
# джобу (её needs: гарантирует, что её тесты кончились), и у нового коммита
# своя джоба deploy стоит за нашей в resource_group. Отказ красил бы старый
# пайплайн красным при здоровом master — выход 0 со строкой хода. Любой
# другой отказ остаётся отказом, в том числе упавший тест нового HEAD.
DEFER = (f"  from-ci: {SHA[:12]} is still being tested; its own pipeline's deploy job "
         f"rolls it out — nothing to do now")
FROM_CI_DEFER = [
    ([f"wait for pipeline {URL} (running)"], DEFER),
    ([f"wait for pipeline {URL} (pending)"], DEFER),
    ([], None),
    ([f"pipeline {URL} is running but job 1 test/unit failed: refusing"], None),
    ([f"pipeline failed for {SHA[:12]}: {URL}"], None),
    ([f"no pipeline for {SHA}"], None),
    ([f"cannot read the pipeline for {SHA}: HTTP 502"], None),
]


def check_from_ci_sync(c):
    """Настоящий git во временном каталоге: fetch и --ff-only (#239)."""
    import subprocess
    import tempfile
    from mop.cli.server import deploy
    fn = getattr(deploy, "ci_sync", None)
    if not c.check("mop server deploy has ci_sync", fn is not None):
        return
    tmp = tempfile.mkdtemp(prefix="mop-ci-sync-")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(cwd, *args):
        return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                              check=True, env=env).stdout.strip()
    origin, work, other = (os.path.join(tmp, n) for n in ("origin.git", "work", "other"))
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", origin], check=True)
    subprocess.run(["git", "clone", "-q", origin, other], check=True, capture_output=True)
    git(other, "commit", "-q", "--allow-empty", "-m", "one")
    git(other, "push", "-q", "origin", "master")
    subprocess.run(["git", "clone", "-q", origin, work], check=True, capture_output=True)
    old = git(work, "rev-parse", "HEAD")
    # Неотслеживаемое (.env, inventory.yaml, бэкапы) — не помеха.
    open(os.path.join(work, ".env"), "w").write("X=1\n")
    c.expect("ci_sync at origin", fn(work), (old, old, None))
    git(other, "commit", "-q", "--allow-empty", "-m", "two")
    git(other, "push", "-q", "origin", "master")
    new = git(other, "rev-parse", "HEAD")
    c.expect("ci_sync behind origin", fn(work), (old, new, None))
    # Разошлась с origin — --ff-only отказывает, и отказ назван.
    git(work, "commit", "-q", "--allow-empty", "-m", "local")
    git(other, "commit", "-q", "--allow-empty", "-m", "three")
    git(other, "push", "-q", "origin", "master")
    got = fn(work)
    c.check("ci_sync diverged: wanted a fast-forward refusal",
            isinstance(got, tuple) and got[2] and got[2].startswith(
                "--from-ci: master does not fast-forward to origin/master"), repr(got))
    # Ветка и грязь — из той же рабочей копии.
    state = getattr(deploy, "ci_state", None)
    if not c.check("mop server deploy has ci_state", state is not None):
        return
    open(os.path.join(work, "tracked"), "w").write("a\n")
    git(work, "add", "tracked")
    git(work, "commit", "-q", "-m", "tracked")
    open(os.path.join(work, "tracked"), "w").write("b\n")
    c.expect("ci_state", state(work), ("master", "master", [" M tracked"]))


# Эпик — не сущность GitLab (она в платной редакции), а маркер в теле задачи:
# первая строка «**Эпик:** #N». Отсюда два требования, и оба молчаливые:
# маркер не должен задваиваться при повторной правке, а смена родителя не
# должна оставлять рядом старую строку — иначе у задачи два родителя и оба
# «настоящие».
EPICS = [
    ("текст", 5, "**Эпик:** #5\n\nтекст"),
    ("**Эпик:** #5\n\nтекст", 5, "**Эпик:** #5\n\nтекст"),
    ("**Эпик:** #4\n\nтекст", 5, "**Эпик:** #5\n\nтекст"),
    ("", 7, "**Эпик:** #7"),
]

EPIC_OF = [
    ("**Эпик:** #5\n\nтекст", 5),
    ("текст\n\n**Эпик:** #5", None),   # только первой строкой: ссылка в теле — не родство
    ("текст", None),
    ("", None),
]


def main():
    c = Checks()
    for url, want in ORIGINS:
        c.expect(f"origin {url!r}", gitlab._parse_origin(url), want)

    for labels, iid, slug, want in BRANCHES:
        c.expect(f"branch {labels} #{iid} {slug!r}", gitlab.branch_name(labels, iid, slug), want)

    for labels, status, want in STATUS:
        got = gitlab.with_status(labels, status)
        c.expect(f"with_status({labels}, {status!r}) (sorted)", sorted(got), sorted(want))

    for state, status, want in RELABEL_ACTION:
        c.expect(f"relabel_action({state!r}, {status!r})",
                 gitlab.relabel_action(state, status), want)

    # Закрыть «как отложенную» нельзя: отложенная открыта (#202).
    c.check("parked must be an open status, not a closing one",
            not ("parked" in gitlab.CLOSE_STATUSES or "parked" not in gitlab.OPEN_STATUSES))

    for pipeline, want in PIPELINE_VERDICT:
        fn = getattr(gitlab, "pipeline_verdict", None)
        got = fn(pipeline, SHA) if fn else "no gitlab.pipeline_verdict"
        c.expect(f"pipeline_verdict({pipeline!r})", got, want)

    for pipeline, jobs, want in VERDICT_JOBS:
        try:
            got = gitlab.pipeline_verdict(pipeline, SHA, jobs)
        except TypeError as e:
            got = f"raised: {e}"
        c.expect(f"pipeline_verdict({pipeline['status']}, {jobs!r})", got, want)

    from mop.cli.server import deploy
    for args, want in FROM_CI:
        fn = getattr(deploy, "from_ci_refusals", None)
        got = fn(*args) if fn else "no deploy.from_ci_refusals"
        c.expect(f"from_ci_refusals{args}", got, want)

    for refusals, want in FROM_CI_DEFER:
        fn = getattr(deploy, "from_ci_deferred", None)
        got = fn(refusals, SHA) if fn else "no deploy.from_ci_deferred"
        c.expect(f"from_ci_deferred({refusals!r})", got, want)

    check_from_ci_sync(c)
    check_deploy_gate(c)
    check_pipeline_by_ref_335(c)

    try:
        gitlab.with_status([], "partial")
        refused = False
    except RuntimeError:
        refused = True
    c.check("an unknown status must refuse, not pass through", refused)

    for name in ("status::nope", "nope::live"):
        try:
            gitlab.check_label(name)
            refused = False
        except RuntimeError:
            refused = True
        c.check(f"check_label({name!r}) must refuse", refused)

    for body, iid, want in EPICS:
        c.expect(f"with_epic({body!r}, {iid})", gitlab.with_epic(body, iid), want)

    for body, want in EPIC_OF:
        c.expect(f"epic_of({body!r})", gitlab.epic_of(body), want)

    c.expect("a label from the vocabulary must pass",
             gitlab.check_label("component::bus"), "component::bus")

    # Правка тела задачи (#288): `mop dev bug edit` заменяет тело целиком, а
    # маркер эпика живёт первой строкой тела -- и терялся бы с каждой правкой,
    # уводя задачу из-под эпика молча. HYPOTHESIS: глагола edit нет, объём
    # задачи фиксируется комментарием, и описание расходится с решением.
    # SOLUTION: gitlab.replace_body(old, new, epic) -- KEEP держит старый
    # маркер, число ставит другой, None снимает. STATUS: FIXED — see #288
    old = "**Эпик:** #281\n\nстарое тело"
    for new, epic, want in [
        ("новое тело", gitlab.KEEP, "**Эпик:** #281\n\nновое тело"),
        ("новое тело", 7, "**Эпик:** #7\n\nновое тело"),
        ("новое тело", None, "новое тело"),
        ("**Эпик:** #9\n\nсвоё", gitlab.KEEP, "**Эпик:** #9\n\nсвоё"),
        ("**Эпик:** #9\n\nсвоё", None, "своё"),
    ]:
        c.expect(f"replace_body(old, {new!r}, {epic!r})",
                 gitlab.replace_body(old, new, epic), want)
    c.expect("replace_body without an epic keeps the body bare",
             gitlab.replace_body("голое тело", "новое", gitlab.KEEP), "новое")
    # Только заголовок -- в запросе нет description: тело не трогается.
    c.expect("edit_payload(title only)", gitlab.edit_payload("Заголовок", None),
             {"title": "Заголовок"})
    c.expect("edit_payload(body only)", gitlab.edit_payload(None, "тело"),
             {"description": "тело"})
    try:
        gitlab.edit_payload(None, None)
        c.fail("edit_payload with nothing to change must refuse")
    except ValueError:
        pass

    return c.report("gitlab")


if __name__ == "__main__":
    sys.exit(main())
