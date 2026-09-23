"""secret variables of the project: mop secret var add|list|remove

  mop secret var add VAR=VALUE...   in the environment of every puppet's session
  mop secret var list               names only
  mop secret var remove VAR...
"""
from mop.cli import lib
from mop import project_secrets
from mop.cli.secret import _common


def main(argv):
    if not argv or argv[0] not in ("add", "list", "remove"):
        lib.usage(__doc__)
    verb, items = argv[0], argv[1:]
    if (verb == "list") != (not items):
        lib.usage(__doc__)
    project = _common.project(__doc__)
    if verb == "list":
        for name in _common.ask(project, "secret_list")["vars"]:
            print(name)
        return 0
    if verb == "remove":
        for name in items:
            _common.ask(project, "secret_remove", kind="var", name=name)
        return 0
    # Разбор -- до первого запроса: опечатка в третьей переменной не должна
    # оставить на сервере первые две.
    try:
        pairs = [project_secrets.parse_var(i) for i in items]
    except ValueError as e:
        lib.usage(str(e))
    for key, value in pairs:
        _common.ask(project, "secret_put", kind="var", key=key, value=value)
    return 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
