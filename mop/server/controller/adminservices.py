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
    """Rootless client отдельной #669 службы; web не строит CryptoProvider."""
    return None
