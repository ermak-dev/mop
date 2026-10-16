"""Реальное восстановление ciphertext с отдельным bootstrap root trust."""
from pathlib import Path
import tempfile

from django.conf import settings
from django.test import TransactionTestCase

from _controller_restore_cases import RestoreSafety
from mop.server.controller import backup
from mop.server.controller.bootstrap import secret_provider
from mop.server.controller.models import Installation
from mop.server.controller.runtime import initialize
from mop.server.secrets import models
from mop.server.secrets.crypto import Envelope
from mop.server.secrets.services import SecretStore, context


# HYPOTHESIS: совпадение root fingerprint не доказывает decryptability архива.
# Restore обязан оставить guard закрытым при повреждённом ciphertext.
# SOLUTION: bounded authentication всех retained versions до clear guard.
# RESULT: PG16 red (restore повреждённого ciphertext принят), затем 104/104 green.
# STATUS: FIXED — see #637
class SecretRecovery(TransactionTestCase):
    serialized_rollback = True
    target = RestoreSafety.target
    assert_restart_denied = RestoreSafety.assert_restart_denied

    def make_secret(self):
        initialize()
        installation = Installation.objects.get(pk=1)
        self.provider = secret_provider(settings.BOOTSTRAP,
            installation_id=installation.identity,
            expected_root_digest=installation.root_digest, key_id='root-v1')
        class FixtureAuthority:
            def authorize(self, *args, **kwargs):
                pass
        self.plaintext = b'synthetic-private-backup-value-637'
        value = SecretStore(provider=self.provider, authorizer=FixtureAuthority()).create(
            None, project_id=None, name='recovery-key', kind='credential',
            purpose='proxy', value=self.plaintext)
        version = models.SecretVersion.objects.get(secret_id=value.id)
        self.assertNotIn(self.plaintext, bytes(version.envelope))
        return version

    def test_native_restore_checks_secret_decryption_before_clearing_guard(self):
        version = self.make_secret()
        damaged = bytes(version.envelope)
        damaged = damaged[:-1] + bytes([damaged[-1] ^ 1])
        # Fault injection обходит application guards, как повреждённый архив.
        models.SecretVersion._base_manager.filter(pk=version.pk).update(envelope=damaged)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'damaged.dump'
            backup.backup(path)
            with self.target() as target:
                with self.assertRaisesRegex(RuntimeError, 'secret|decrypt'):
                    backup.restore(path, using=target)
                self.assertFalse(Installation.objects.using(target).get(pk=1).effects_enabled)
                self.assert_restart_denied(target)

    def test_native_secret_restore_roundtrips_with_separate_protected_root(self):
        version = self.make_secret()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'encrypted.dump'
            backup.backup(path)
            with self.target() as target:
                result = backup.restore(path, using=target)
                self.assertFalse(result['effects_enabled'])
                recovered = models.SecretVersion.objects.using(target).select_related(
                    'secret__installation').get(pk=version.pk)
                self.assertEqual(self.provider.decrypt(Envelope.from_bytes(bytes(recovered.envelope)),
                    context(recovered.secret, recovered)), self.plaintext)
