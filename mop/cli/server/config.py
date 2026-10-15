"""settings of this installation: mop server config [--json | set KEY VALUE]

Shows what the system runs on right now and where each value came from: an
environment variable outranks .env, .env outranks the default in the code.

Defaults intentionally equal the cluster's current values: .env is in
.gitignore, and a fresh clone without it must still come up as-is. The only
things required in .env are secrets.

set KEY VALUE changes one declared setting in the controller's .env,
atomically and without displaying its value. It does not reconfigure running
services: apply the installation through the normal deployment gate.
MOP_SOUL_ROLES accepts 1 to enable or an empty value to disable.
"""
import json
import os

from mop.cli import lib
from mop.common import config
from mop.server import natsconf, playvars, settingsedit
from mop.cli.server._maintenance import require as require_controller
from mop.common.render import table


def lock_file():
    """Замок установки рядом с локальными служебными учётками контроллера."""
    return os.path.join(os.path.dirname(natsconf.BASE), "settings.lock")


def main(argv):
    if len(argv) == 3 and argv[0] == "set":
        require_controller()
        settingsedit.set_value(config.ENV_FILE, argv[1], argv[2],
                               config.SETTINGS, lock_file())
        config.forget()
        return 0
    eff = config.effective()
    if argv == ["--json"]:
        # Так это уезжает в плейбуки: `mop server deploy` отдаёт их --extra-vars.
        # Состав -- playvars.playbook_vars(), тот же, что у сборки образа.
        print(json.dumps(playvars.playbook_vars(), ensure_ascii=False))
        return
    if argv:
        lib.usage(__doc__)
    print("\n".join(table([("SETTING", "VALUE", "SOURCE")]
                          + [(k, "[redacted]" if k == "MOP_PROXY_KEY" else v, src)
                             for k, (v, src) in eff.items()])))
    print(f"\nfile: {config.ENV_FILE}")


