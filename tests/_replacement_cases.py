"""Замена инкарнации (#625): узкие контракты до сборки update.

HYPOTHESIS: nonzero register принимает один индекс без точного predecessor,
а чтение старой/чужой Meta после потерянного POST объявляет отказ. Поздний
POST тогда может примениться уже после снятия fences.
SOLUTION: typed predecessor для nonzero CAS; только preflight/атомарный 409
доказывают отказ, readback после возможного dispatch оставляет unknown.
Проверяется PostgreSQL gate и owned HTTP peer, не настоящий Nomad.
RESULT: RED -- два поведенческих отказа и отсутствие predecessor API; GREEN --
15/15 replacement/create registration, Python 3.12 и PostgreSQL 17.
STATUS: FIXED — see #625
"""
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from django.test import TransactionTestCase
from django.utils import timezone

from _register_peer import RegisterPeer
from mop.server.operations.puppet import RestartTarget
from mop.server.operations.scheduler import (JobPin, NomadStopScheduler, RegisterTicket,
                                              addressed_metadata)


class ReplacementRegistration(TransactionTestCase):
    serialized_rollback = True

    def setUp(self):
        self.peer = RegisterPeer()
        self.addCleanup(self.peer.close)
        self.scheduler = NomadStopScheduler(origin=self.peer.origin, scheduler_id=uuid4(),
                                            token='fixture-only', timeout=.2)
        self.epoch, self.restore = uuid4(), uuid4()
        self.source = RestartTarget(uuid4(), uuid4(), uuid4(), uuid4(), uuid4(),
                                    1, 'worker', 'a'*64, uuid4(), 2, 2)
        self.destination = replace(self.source, incarnation_id=uuid4(), grant_id=uuid4(), generation=3, revision=3)
        self.ticket = RegisterTicket(self.epoch, self.destination, self.scheduler.scheduler_id,
            str(self.source.target_id), 'default', self.restore, 0,
            {'ID': str(self.source.target_id), 'Meta': addressed_metadata(self.epoch, self.restore, self.destination)},
            uuid4(), uuid4(), timezone.now()+timedelta(minutes=1))
        self.pin = JobPin(self.epoch, self.source, self.scheduler.scheduler_id,
                         self.ticket.job_id, 'default', 7, self.restore)

    def old_job(self, **changes):
        value = {'ID': self.ticket.job_id, 'Namespace': 'default', 'JobModifyIndex': 7,
                 'Meta': addressed_metadata(self.epoch, self.restore, self.source), 'Stop': False}
        value.update(changes)
        return value

    def replacement(self):
        return replace(self.ticket, expected_index=7, predecessor=self.pin)

    def test_nonzero_index_without_exact_predecessor_is_not_authority(self):
        with self.assertRaises(ValueError):
            replace(self.ticket, expected_index=7)

    def test_replacement_has_one_nonzero_conditional_post(self):
        self.peer.jobs[self.ticket.job_id] = self.old_job()
        proof = self.scheduler.register_job(self.replacement())
        self.assertEqual((proof.outcome, proof.modify_index), ('succeeded', 8))
        self.assertEqual([(p['EnforceIndex'], p['JobModifyIndex']) for p in self.peer.posts], [(True, 7)])
        self.assertEqual(self.peer.jobs[self.ticket.job_id]['Meta'], self.ticket.job['Meta'])

    def test_foreign_predecessor_at_the_same_index_is_refused_before_post(self):
        self.peer.jobs[self.ticket.job_id] = self.old_job(Meta={**self.old_job()['Meta'], 'mop.grant_id': str(uuid4())})
        self.assertEqual(self.scheduler.register_job(self.replacement()).outcome, 'failed')
        self.assertEqual(self.peer.posts, [])

    def test_predecessor_identity_must_match_the_conditional_registration(self):
        for changes in ({'modify_index': 8}, {'scheduler_id': uuid4()}, {'namespace': 'other'},
                        {'job_id': 'foreign'}, {'restore_generation': uuid4()}, {'epoch': uuid4()},
                        {'target': replace(self.source, target_id=uuid4())}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.ticket, expected_index=7, predecessor=replace(self.pin, **changes))

    def test_create_index_zero_never_accepts_a_predecessor_or_overwrites_a_job(self):
        with self.assertRaises(ValueError):
            replace(self.ticket, predecessor=self.pin)
        self.peer.jobs[self.ticket.job_id] = self.old_job()
        self.assertEqual(self.scheduler.register_job(self.ticket).outcome, 'failed')
        self.assertEqual(self.peer.posts, [])

    def test_foreign_readback_after_possible_dispatch_is_unknown_not_failed(self):
        self.peer.jobs[self.ticket.job_id] = self.old_job()
        self.assertEqual(self.scheduler.inspect_register(self.ticket).outcome, 'unknown')

    def test_delayed_post_old_readback_cannot_release_the_replacement_fence(self):
        ticket = self.replacement()
        self.peer.jobs[ticket.job_id] = self.old_job()
        self.peer.defer_post = True
        self.peer.lose_post = True
        self.assertEqual(self.scheduler.register_job(ticket).outcome, 'unknown')
        self.assertTrue(self.peer.post_arrived.is_set(), 'the POST must really have reached the owned peer')
        self.assertEqual(self.scheduler.inspect_register(ticket).outcome, 'unknown')
        self.assertEqual(self.peer.jobs[ticket.job_id]['JobModifyIndex'], 7)
        self.peer.release_post.set()
        self.assertTrue(self.peer.post_applied.wait(2), 'the same original POST must apply late')
        self.assertEqual(self.scheduler.inspect_register(ticket).outcome, 'succeeded')
        self.assertEqual(len(self.peer.posts), 1)
