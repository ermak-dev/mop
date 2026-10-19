"""Composition Admin, без root key и без второго исполнителя операций.

Frozen #625 даёт настоящий admission/status и отдельный worker adapter, но
production worker ещё не связан. #644 связывает конечные handlers и защищённый
CredentialClient при установке; отсутствие связи видно до показа submit.
"""
from .authviews import catalog_api
from .runtime import operations_service, installation_snapshot


def node_actions():
    """Только связанные effect handlers; наличие permission не является binding."""
    return ()


def credential_client():
    """Rootless client отдельной службы; construction не доказывает readiness.

    Точные UID/path берёт installer bootstrap. Никакого root/provider import,
    повторного OS lookup, кэшированного bearer или fallback к legacy inputs.
    """
    from django.conf import settings
    from mop.common.credential_client import CredentialClient
    from .bootstrap import credential_endpoint
    endpoint = credential_endpoint(settings.BOOTSTRAP)
    if endpoint is None:
        return None
    return CredentialClient(endpoint['path'], server_uid=endpoint['server_uid'], timeout=5)
