"""Полный PostgreSQL gate foundation и каталога без внешней test-транзакции."""
import sys
import _lib  # noqa: F401 — lifetime временных файлов
import hermetic  # noqa: F401 — до framework и mop
from _controller_fixture import main


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:] or ['_ldap_cases', '_access_cases', '_controller_cases', '_controller_restore_cases', '_catalog_cases', '_catalog_retirement_cases', '_secret_cases', '_secret_recovery_cases.SecretRecovery', '_grant_cases', '_operation_cases', '_execution_cases', '_restart_vertical_cases', '_restart_live_cases', '_telemetry_cases', '_realtime_cases', '_endpoint_cases', '_endpoint_grant_cases', '_credential_cases', '_preparation_cases', '_admin_cases', '_build_cases.ImageBuilds', '_projection_cases', '_proxy_usage_cases', '_gateway_cases']))
