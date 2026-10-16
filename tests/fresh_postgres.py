"""Fresh install gate в новой PostgreSQL fixture; не проверка живой установки."""
import sys
import _lib  # noqa: F401
import hermetic  # noqa: F401
from _controller_fixture import main

if __name__ == '__main__':
    sys.exit(main(['_installation_cases', '_installation_acl_cases.InstallationACL']))
