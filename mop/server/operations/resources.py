"""Публичный SQL-only query общего fence; чужие области не пишут claims."""
from .models import ResourceClaim


def target_held(target_id, *, excluding_operation=None):
    claims = ResourceClaim.objects.filter(resource_id='target:'+str(target_id), released_at=None)
    if excluding_operation is not None:
        claims = claims.exclude(operation_id=excluding_operation)
    return claims.exists()
