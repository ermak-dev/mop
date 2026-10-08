"""launch claude outside the pool: mop code [claude options]

Same session environment as `mop master`, none of the pool. No project, no
bus credentials, no mop MCP server: this is an ordinary claude session on
the installation's LLM proxy. Use it where the pool has nothing to do with
the job — a scratch checkout, someone else's repository, a shell on a
machine that never got `mop server deploy`.

Everything goes to claude as-is
(`mop code --continue` resumes the last session here). The command won't
mirror claude's own flags: there are dozens of them, and the list would drift
the moment claude ships a new version.

Permissions are skipped, same as `mop master` and the puppets: the session
comes up with --dangerously-skip-permissions added for you. The whole point
of the command is to get to work on a provider without ceremony, and
answering a prompt per shell call is exactly the ceremony. Passing the flag
yourself is harmless — it isn't added twice.

The session uses the selected server's LLM proxy. Its URL and key come from
the server profile, connection.json saved by mop login; missing credentials
start login before claude. With multiple joined servers, name one with
--server. Without a terminal, missing input refuses. This command never adds
the pool's MCP tools.
"""
import os

from mop.cli import lib
from mop.cli.core import _common
from mop.cli.pool import login


def claude_argv(passthru, settings_path=None):
    """Аргументы запуска claude -> список. Чистая функция (#419): и флаг
    пропуска разрешений, и отключение черновиков отзыва живут здесь, у
    фактического execvpe -- одно место на оба.

    Настройки отзыва -- ОДИН --settings сразу за «claude»: повторных
    claude не мержит (побеждает последний), за `--` флаги не читаются, а
    вставка в середину рвала бы чужой флаг с значением. Авто-skip (когда
    пользователь не передал свой) -- сразу после пары. settings_path --
    memfd-путь настроек (commandlet готовит), без него своя пара."""
    skip = "--dangerously-skip-permissions"
    settings = ["--settings", settings_path] if settings_path \
        else list(_common.FEEDBACK_SETTINGS)
    args = ([] if skip in passthru else [skip]) + list(passthru)
    return ["claude", *settings, *args]


def main(argv):
    login.ensure()
    # Никакого разбора позиционных: своих аргументов у команды нет, и всё
    # уезжает claude дословно. Отсюда же отсутствие обязательного
    # `--` из `mop master`: там он отделял origin от значения чужого флага,
    # здесь отделять не от чего.
    passthru = argv
    # Тот же joined-конфиг, что у мастера, без доступа к пулу.
    env = _common.launch_env()
    # Настройки отзыва (#419): пользовательский --settings (если был)
    # сливается с feedbackDrafts=off; итог ездит memfd-путём -- без
    # argv-содержимого и без диска. Недоступный файл или не-JSON --
    # отказ, а не молчаливое включение черновиков.
    try:
        value, passthru = _common.feedback_settings(passthru)
    except ValueError as e:
        lib.usage(str(e))
    path, _fd = _common.feedback_fd(value)
    # flush до exec: буфер stdout не переживает execvpe, и строка о профиле
    # пропадала бы везде, где вывод не в терминал.
    # Флаг добавляется здесь, а не оставляется пользователю: команда для
    # того и есть, чтобы сесть за работу без церемоний. Свой экземпляр из
    # passthru не дублируем — claude второй раз не нужен.
    args = claude_argv(passthru, path)[1:]
    print(f"claude + {' '.join(args)}", flush=True)
    os.execvpe("claude", ["claude"] + args, env)


# Не lib.cluster: пула эта команда не касается, и требовать настроенный
# кластер ради запуска claude в чужом каталоге не за что.
