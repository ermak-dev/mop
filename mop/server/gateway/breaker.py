"""Межпроцессный circuit: короткий row lock, HTTP только после COMMIT.

Все management calls используют одну полосу: даже proven ключ может стать
невалидным между двумя запросами. Это намеренно строже локального KeyBreaker:
параллельные worker не успевают отправить пять отказов до первого результата.
Deadline не доказывает quiescence; просроченный claim не переисполняется.
"""
from datetime import timedelta
import uuid
from django.db import transaction
from django.utils import timezone
from .models import Circuit, KeyState
from .values import Claim


class Breaker:
    def __init__(self, *, clock=timezone.now, deadline_seconds=15, cooldown_seconds=1800):
        if not 0 < deadline_seconds <= 75 or not 1800 <= cooldown_seconds <= 86400:
            raise ValueError('invalid proxy circuit deadlines')
        self.clock = clock
        self.deadline_seconds = deadline_seconds
        self.cooldown_seconds = cooldown_seconds

    def claim(self, credential, *, write):
        now = self.clock()
        fingerprint = credential.fingerprint
        with transaction.atomic():
            Circuit.objects.get_or_create(pk=1)
            row = Circuit.objects.select_for_update().get(pk=1)
            if row.state != 'idle':
                return Claim('unknown' if row.state == 'unknown' or row.deadline <= now else 'waiting')
            key, _ = KeyState.objects.get_or_create(pk=fingerprint,
                defaults={'version_id': credential.version_id})
            if key.state == 'rejected':
                return Claim('rejected')
            if row.cooldown_until and now < row.cooldown_until:
                return Claim('cooldown')
            row.claim_id = uuid.uuid4()
            row.claim_fingerprint = fingerprint
            row.version_id = credential.version_id
            row.deadline = now + timedelta(seconds=self.deadline_seconds)
            row.write, row.state = write, 'running'
            row.revision += 1
            row.save()
            return Claim('claimed', row.claim_id, row.revision, fingerprint)

    def complete(self, claim, status):
        with transaction.atomic():
            row = Circuit.objects.select_for_update().get(pk=1)
            if (row.claim_id, row.revision, row.claim_fingerprint) != (claim.id, claim.revision, claim.fingerprint):
                raise RuntimeError('proxy receipt does not match current claim')
            key = KeyState.objects.select_for_update().get(pk=claim.fingerprint)
            if status in (401, 403):
                key.state = 'rejected'
                if status == 403:
                    row.cooldown_until = self.clock() + timedelta(seconds=self.cooldown_seconds)
            elif status is not None and 200 <= status < 300:
                key.state = 'proven'
            key.version_id, key.revision = row.version_id, key.revision + 1
            key.save()
            if status is None and row.write:
                # Отсутствие HTTP receipt не означает, что PUT не применился.
                row.state = 'unknown'
            else:
                row.state, row.claim_id, row.deadline = 'idle', None, None
                row.claim_fingerprint = ''
            row.revision += 1
            row.save()
