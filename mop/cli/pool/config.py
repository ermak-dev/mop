"""settings of this installation: mop config [--json]

Shows what the system runs on right now and where each value came from: an
environment variable outranks .env, .env outranks the default in the code.

Defaults intentionally equal the cluster's current values: .env is in
.gitignore, and a fresh clone without it must still come up as-is. The only
things required in .env are secrets.
"""
import json

from mop.cli import lib
from mop import config, playvars
from mop.render import table


def main(argv):
    eff = config.effective()
    if argv == ["--json"]:
        # Так это уезжает в плейбуки: `mop deploy` отдаёт их --extra-vars.
        # Состав -- playvars.playbook_vars(), тот же, что у сборки образа.
        print(json.dumps(playvars.playbook_vars(), ensure_ascii=False))
        return
    if argv:
        lib.usage(__doc__)
    print("\n".join(table([("SETTING", "VALUE", "SOURCE")]
                          + [(k, v, src) for k, (v, src) in eff.items()])))
    print(f"\nfile: {config.ENV_FILE}")


