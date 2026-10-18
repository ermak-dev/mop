// HYPOTHESIS: независимые ответы REST/WS откатывают подтверждённые версии (#627).
// SOLUTION: одна чистая граница, версии строками и токены поколения/снимка.
// RESULT: проверены перестановки REST/WS, presence-токены, reset и границы буферов.
// STATUS: FIXED — see #627 (только чистое ядро; сетевые/UI-проверки отдельно).
import { describe, expect, it } from 'vitest';
import {
  beginRead, createPoolState, entityKey, reducePool, recoveryRequests,
  type Context, type Entity, type PoolState, type ReadQuery,
} from './poolStore';

const context: Context = { server: 'https://controller', user: 'u1', scope: 'p1', authorization: 'a1', credentialGeneration: 'session-1', target: 'p' };
const puppet = (revision = '1', incarnation = 'i1', id = 'p'): Entity =>
  ({ kind: 'puppet', id, incarnation, revision, data: { name: id, facts: { fresh: true } } });
const operation = (revision = '1', status = 'accepted'): Entity => ({
  kind: 'operation', id: 'o', incarnation: null, revision, data: {},
  operation: { requestId: 'r', status, target: { id: 'p', incarnation: 'i1' } },
});
const key = entityKey(puppet());
const initial = () => createPoolState(context, 'e1');
function read(state: PoolState, rows: Entity[], query: ReadQuery = { type: 'detail' }) {
  const request = beginRead(state, query);
  return reducePool(request.state, { type: 'rest', token: request.token, epoch: 'e1', rows });
}
function event(state: PoolState, revision: string, options: Partial<Entity> = {}, eventId = `ev-${revision}`) {
  const { data: _data, operation: _op, ...entity } = { ...puppet(revision), ...options };
  return reducePool(state, { type: 'event', generation: state.generation, epoch: 'e1', eventId, entity });
}
function snapshot(state: PoolState) {
  return beginRead(state, { type: 'snapshot', kind: 'puppet' });
}

describe('versioned pool core #627', () => {
  // HYPOTHESIS: epoch bootstrap отменяет ранний 403 того же credential.
  // SOLUTION: первое связывание epoch сохраняет текущие read tokens; смена -- reset.
  // STATUS: FIXED — see #627
  it('binds the first epoch without abandoning concurrent authority reads, never rebinds stale contexts', () => {
    const reading = beginRead(createPoolState(context, ''), { type: 'detail' });
    const bootstrap = (epoch: string, generation = reading.state.generation) => ({ type: 'bootstrap', generation, epoch } as Parameters<typeof reducePool>[1]);
    const bound = reducePool(reading.state, bootstrap('e1'));
    expect(bound.epoch).toBe('e1'); expect(bound.generation).toBe(reading.state.generation);
    expect(bound.requests).toEqual(reading.state.requests);
    const changed = reducePool(bound, bootstrap('e2'));
    expect(changed.generation).toBeGreaterThan(bound.generation); expect(changed.requests).toEqual({});
    expect(reducePool(changed, bootstrap('e1'))).toBe(changed);
  });
  // HYPOTHESIS: одинаковые user/permissions не означают ту же credential;
  // серверный next cursor может зациклить конечный fetch-loop без page budget.
  // SOLUTION: credential generation — часть auth scope; visited cursors/page cap.
  // RESULT: четыре behavioral red до исправления, затем green.
  // STATUS: FIXED — see #627; это core boundary, не приёмка browser migration.
  it('credential generation change clears private entities and pending commands before late logout-era HTTP', () => {
    const request = beginRead(read(initial(), [puppet(), operation()]), { type: 'detail' });
    let state = reducePool(request.state, { type: 'track', requestId: 'lost-before-login' });
    const changedCredential = { ...context, credentialGeneration: 'new-django-session' };
    state = reducePool(state, { type: 'context', context: changedCredential, epoch: 'e1' });
    expect(state.entities).toEqual({});
    expect(recoveryRequests(state)).toEqual([]);
    const cleared = state;
    state = reducePool(state, { type: 'rest', token: request.token, epoch: 'e1', rows: [puppet('9')] });
    expect(state).toBe(cleared);
  });

  it.each([['a', 'a'], ['a', 'b', 'a']])('rejects cyclic snapshot continuation %s instead of an unbounded HTTP loop', (...cursors) => {
    let state = initial();
    let req = snapshot(state);
    state = reducePool(req.state, { type: 'rest', token: req.token, epoch: 'e1', rows: [],
      page: { snapshotId: 's', cursor: 'watermark', next: cursors[0] } });
    for (let i = 1; i < cursors.length; i++) {
      req = beginRead(state, { type: 'snapshot', kind: 'puppet', snapshotId: 's', cursor: cursors[i - 1] });
      state = reducePool(req.state, { type: 'rest', token: req.token, epoch: 'e1', rows: [],
        page: { snapshotId: 's', cursor: 'watermark', next: cursors[i] } });
    }
    expect(state.resync).toBe('snapshot_mismatch');
    expect(state.entities).toEqual({});
  });

  it('bounds snapshot pages even when server invents a fresh opaque continuation on every empty page', () => {
    // Структурный тип допускает будущий budget без import/setup failure.
    const limits = { maxPages: 2 };
    let state = createPoolState(context, 'e1', limits);
    const first = snapshot(state);
    state = reducePool(first.state, { type: 'rest', token: first.token, epoch: 'e1', rows: [],
      page: { snapshotId: 's', cursor: 'watermark', next: 'a' } });
    const second = beginRead(state, { type: 'snapshot', kind: 'puppet', snapshotId: 's', cursor: 'a' });
    state = reducePool(second.state, { type: 'rest', token: second.token, epoch: 'e1', rows: [],
      page: { snapshotId: 's', cursor: 'watermark', next: 'b' } });
    expect(state.resync).toBe('overflow');
    expect(state.connection.stale).toBe(true);
  });

  it('fences stale connection errors, reset instructions and abandoned reads', () => {
    const request = beginRead(initial(), { type: 'detail' });
    let state = reducePool(request.state, { type: 'context', context: { ...context, user: 'other' }, epoch: 'e1' });
    const current = state;
    state = reducePool(state, { type: 'connection', generation: request.token.generation, status: 'disconnected', error: 'old auth error' });
    state = reducePool(state, { type: 'reset', generation: request.token.generation, reason: 'cursor_unknown' });
    expect(state).toBe(current);
  });

  it('settles failed or abandoned reads without erasing confirmed domain state', () => {
    let state = read(initial(), [puppet()]);
    const abandoned = beginRead(state, { type: 'detail' });
    state = reducePool(abandoned.state, { type: 'settled', token: abandoned.token });
    expect(Object.keys(state.requests)).toEqual([]);
    const late = reducePool(state, { type: 'rest', token: abandoned.token, epoch: 'e1', rows: [puppet()] });
    expect(late).toBe(state);
  });

  for (const order of [[0, 1, 2], [0, 2, 1], [1, 0, 2], [1, 2, 0], [2, 0, 1], [2, 1, 0]]) {
    it(`converges REST/WS permutation ${order.join('-')} at the greatest confirmed revision`, () => {
      const first = beginRead(initial(), { type: 'detail' });
      const second = beginRead(first.state, { type: 'detail' });
      let state = second.state;
      for (const item of order) {
        state = item === 2 ? event(state, '3') : reducePool(state, {
          type: 'rest', token: item === 0 ? first.token : second.token, epoch: 'e1', rows: [puppet(item === 0 ? '2' : '4')],
        });
      }
      expect(state.entities[key].value?.revision).toBe('4');
      expect(state.entities[key].invalidated).toBe(false);
    });
  }

  it('materializes a REST entity without modifying an older snapshot', () => {
    const before = initial();
    const after = read(before, [puppet()]);
    expect(after.entities[key].value).toEqual(puppet());
    expect(before.entities).toEqual({});
    expect(after).not.toBe(before);
  });

  it('clones nested input and exposes deeply frozen snapshots and tokens', () => {
    const row = puppet();
    const req = beginRead(initial(), { type: 'detail' });
    const after = reducePool(req.state, { type: 'rest', token: req.token, epoch: 'e1', rows: [row] });
    (row.data.facts as { fresh: boolean }).fresh = false;
    expect(after.entities[key].value?.data.facts).toEqual({ fresh: true });
    expect(Object.isFrozen(after.entities[key].value?.data.facts)).toBe(true);
    expect(Object.isFrozen(req.token)).toBe(true);
    expect(Object.isFrozen(after.context)).toBe(true);
  });

  it('compares arbitrary decimal revisions beyond 2^53, including zero', () => {
    let state = read(initial(), [puppet('0')]);
    expect(state.entities[key].value?.revision).toBe('0');
    state = read(state, [puppet('9007199254740993')]);
    state = read(state, [puppet('9007199254740992')]);
    expect(state.entities[key].value?.revision).toBe('9007199254740993');
    state = read(state, [puppet('100000000000000000000000000000')]);
    expect(state.entities[key].value?.revision).toBe('100000000000000000000000000000');
  });

  for (const order of ['event-first', 'rest-first']) {
    it(`keeps invalidation floor for ${order}; an event is not an entity payload`, () => {
      let state = read(initial(), [puppet('1')]);
      const req = beginRead(state, { type: 'detail' });
      state = req.state;
      if (order === 'event-first') state = event(state, '3');
      state = reducePool(state, { type: 'rest', token: req.token, epoch: 'e1', rows: [puppet('2')] });
      if (order === 'rest-first') state = event(state, '3');
      expect(state.entities[key].head.revision).toBe('3');
      expect(state.entities[key].invalidated).toBe(true);
      expect(state.entities[key].value?.revision).not.toBe('3');
      state = read(state, [puppet('3')]);
      expect(state.entities[key].invalidated).toBe(false);
      expect(state.entities[key].value?.revision).toBe('3');
    });
  }

  it('does not roll back newer REST with an older request or duplicate event', () => {
    const older = beginRead(initial(), { type: 'detail' });
    let state = read(older.state, [puppet('5')]);
    state = reducePool(state, { type: 'rest', token: older.token, epoch: 'e1', rows: [puppet('2')] });
    state = event(state, '3');
    expect(state.entities[key].value?.revision).toBe('5');
    expect(state.entities[key].invalidated).toBe(false);
    const duplicate = event(state, '3');
    expect(duplicate).toBe(state);
  });

  for (const source of ['rest', 'event']) {
    it(`retains ${source} tombstones against stale REST and late events`, () => {
      let state = read(initial(), [puppet('2')]);
      const req = beginRead(state, { type: 'detail' });
      state = source === 'event' ? event(req.state, '4', { deleted: true }) : read(req.state, [{ ...puppet('4'), deleted: true }]);
      state = reducePool(state, { type: 'rest', token: req.token, epoch: 'e1', rows: [puppet('3')] });
      state = read(state, [puppet('4')]);
      state = event(state, '3');
      expect(state.entities[key].head.deleted).toBe(true);
      expect(state.entities[key].value).toBeUndefined();
    });
  }

  it('fences a retired incarnation even if its delayed response claims a larger revision', () => {
    let state = read(initial(), [puppet('1')]);
    state = event(state, '3', { incarnation: 'i2' });
    expect(state.entities[key].value).toBeUndefined();
    state = read(state, [puppet('2')]);
    state = read(state, [puppet('3', 'i2')]);
    state = read(state, [puppet('99', 'i1')]);
    expect(state.entities[key].value?.incarnation).toBe('i2');
    expect(state.entities[key].head.revision).toBe('3');
  });

  it('rejects conflicting equal revisions and malformed decimal versions with explicit resync', () => {
    let state = read(initial(), [puppet('1')]);
    state = event(state, '1', { incarnation: 'i2' });
    expect(state.resync).toBe('version_conflict');
    for (const revision of ['-1', '1.5', '1e4', '01', '', 'NaN']) {
      expect(read(initial(), [puppet(revision)]).resync).toBe('invalid_revision');
    }
  });

  it('requires actual decimal strings, not JSON numbers already rounded by JavaScript', () => {
    expect(read(initial(), [puppet(9007199254740992 as unknown as string)]).resync).toBe('invalid_revision');
  });

  it('resets on REST epoch mismatch without exposing any of its payload', () => {
    const req = beginRead(read(initial(), [puppet()]), { type: 'detail' });
    const state = reducePool(req.state, { type: 'rest', token: req.token, epoch: 'e2', rows: [puppet('5')] });
    expect(state.resync).toBe('epoch_mismatch');
    expect(state.entities).toEqual({});
    expect(state.epoch).toBe('e2');
  });

  it('bounds retired incarnations even when every new incarnation is fetched immediately', () => {
    let state = createPoolState(context, 'e1', { maxInvalidations: 1 });
    state = read(state, [puppet('1')]);
    state = read(state, [puppet('2', 'i2')]);
    state = read(state, [puppet('3', 'i3')]);
    expect(state.resync).toBe('overflow');
  });

  for (const order of [[0, 1, 2], [0, 2, 1], [1, 0, 2], [1, 2, 0], [2, 0, 1], [2, 1, 0]]) {
    it(`never resurrects a tombstone in permutation ${order.join('-')}`, () => {
      let state = initial();
      for (const item of order) {
        state = item === 1 ? event(state, '3', { deleted: true }) : read(state, [puppet(item === 0 ? '1' : '2')]);
      }
      expect(state.entities[key].head.deleted).toBe(true);
      expect(state.entities[key].value).toBeUndefined();
    });
  }

  it('merges operation GET before accepted response by identity without regressing success', () => {
    let state = reducePool(initial(), { type: 'track', requestId: 'r' });
    const accepted = beginRead(state, { type: 'detail' });
    state = read(accepted.state, [operation('3', 'succeeded')]);
    state = reducePool(state, { type: 'rest', token: accepted.token, epoch: 'e1', rows: [operation()] });
    expect(state.entities[entityKey(operation())].value?.operation?.status).toBe('succeeded');
    expect(recoveryRequests(state)).toEqual([]);
  });

  it('event before accepted response invalidates operation and requires GET, not inferred success', () => {
    let state = reducePool(initial(), { type: 'track', requestId: 'r' });
    state = event(state, '3', { kind: 'operation', id: 'o', incarnation: null });
    state = read(state, [operation()]);
    expect(state.entities[entityKey(operation())].value).toBeUndefined();
    expect(recoveryRequests(state)).toEqual(['r']);
    state = read(state, [operation('3', 'running')]);
    expect(recoveryRequests(state)).toEqual(['r']);
  });

  it('keeps lost accepted/result request identities across same-scope reset, never synthesizes results', () => {
    let state = reducePool(initial(), { type: 'track', requestId: 'lost-accepted' });
    state = reducePool(state, { type: 'track', requestId: 'r' });
    state = read(state, [operation()]);
    expect(state.entities[entityKey(operation())].value?.operation?.status).toBe('accepted');
    state = reducePool(state, { type: 'connection', generation: state.generation, status: 'disconnected', error: 'unavailable' });
    expect(recoveryRequests(state)).toEqual(['lost-accepted', 'r']);
    state = reducePool(state, { type: 'reset', generation: state.generation, reason: 'cursor_expired' });
    expect(recoveryRequests(state)).toEqual(['lost-accepted', 'r']);
    expect(state.entities).toEqual({});
    state = read(state, [operation('4', 'failed')]);
    expect(recoveryRequests(state)).toEqual(['lost-accepted']);
  });

  for (const field of ['user', 'scope', 'server', 'authorization', 'target'] as const) {
    it(`changes generation and rejects late responses on ${field} context change`, () => {
      const request = beginRead(read(initial(), [puppet()]), { type: 'detail' });
      let state = reducePool(request.state, { type: 'track', requestId: 'r' });
      state = reducePool(state, { type: 'context', context: { ...context, [field]: 'other' }, epoch: 'e1' });
      const changed = state;
      state = reducePool(state, { type: 'rest', token: request.token, epoch: 'e1', rows: [puppet('5')] });
      state = reducePool(state, { type: 'event', generation: request.token.generation, epoch: 'e1', eventId: 'old', entity: puppet('6') });
      expect(state).toBe(changed);
      expect(state.entities).toEqual({});
      expect(recoveryRequests(state)).toEqual(field === 'target' ? ['r'] : []);
    });
  }

  it('resets on epoch mismatch and fences the old event connection', () => {
    let state = read(initial(), [puppet()]);
    const generation = state.generation;
    state = reducePool(state, { type: 'event', generation, epoch: 'e2', eventId: 'new', entity: puppet('2') });
    expect(state.resync).toBe('epoch_mismatch');
    expect(state.epoch).toBe('e2');
    expect(state.entities).toEqual({});
    const reset = state;
    state = reducePool(state, { type: 'event', generation, epoch: 'e1', eventId: 'late', entity: puppet('3') });
    expect(state).toBe(reset);
  });

  for (const reason of ['cursor_unknown', 'cursor_expired', 'gap', 'overflow'] as const) {
    it(`explicitly resyncs ${reason} instead of reporting an empty successful list`, () => {
      const before = read(initial(), [puppet()]);
      const after = reducePool(before, { type: 'reset', generation: before.generation, reason });
      expect(after.generation).toBeGreaterThan(before.generation);
      expect(after.resync).toBe(reason);
      expect(after.connection.stale).toBe(true);
    });
  }

  it('bounds duplicate event IDs, outstanding reads, and invalidation/retirement metadata', () => {
    let state = createPoolState(context, 'e1', { maxEvents: 2, maxInvalidations: 2, maxReads: 2 });
    state = event(state, '1');
    state = event(state, '2');
    state = event(state, '3');
    expect(state.resync).toBe('overflow');
    state = createPoolState(context, 'e1', { maxEvents: 10, maxInvalidations: 1, maxReads: 2 });
    state = event(state, '1');
    state = event(state, '1', { id: 'p2' }, 'other');
    expect(state.resync).toBe('overflow');
    state = initial();
    state = createPoolState(context, 'e1', { maxReads: 1 });
    state = beginRead(state, { type: 'detail' }).state;
    expect(beginRead(state, { type: 'detail' }).state.resync).toBe('overflow');
  });

  it('keeps connection failures separate from roster and server operation states', () => {
    let state = read(initial(), [puppet(), operation()]);
    const entities = state.entities;
    state = reducePool(state, { type: 'connection', generation: state.generation, status: 'disconnected', error: 'safe reason' });
    expect(state.entities).toEqual(entities);
    expect(state.connection).toEqual({ status: 'disconnected', stale: true, error: 'safe reason' });
    state = reducePool(state, { type: 'connection', generation: state.generation, status: 'connected' });
    expect(state.connection.stale).toBe(true);
    state = reducePool(state, { type: 'synced', generation: state.generation });
    expect(state.connection.stale).toBe(false);
  });

  it('does not remove a newer detail/event row merely absent from an older snapshot', () => {
    let state = read(initial(), [puppet('1', 'i1', 'old')]);
    const req = snapshot(state);
    state = read(req.state, [puppet('4')]);
    state = event(state, '5', { id: 'event-only' });
    state = reducePool(state, { type: 'rest', token: req.token, epoch: 'e1', rows: [], page: { snapshotId: 's1', cursor: 'opaque-z', next: null } });
    expect(state.collections.puppet.ids).toContain(key);
    expect(state.collections.puppet.ids).toContain(entityKey(puppet('5', 'i1', 'event-only')));
    expect(state.collections.puppet.ids).not.toContain(entityKey(puppet('1', 'i1', 'old')));
    expect(state.entities[key].value?.revision).toBe('4');
  });

  it('protects presence confirmed by equal-version WS after an older detail completes', () => {
    // HYPOTHESIS: equal-revision early return теряет новое подтверждение
    // присутствия и позволяет старому пустому snapshot скрыть созданную сущность.
    const detail = beginRead(initial(), { type: 'detail' });
    const req = snapshot(detail.state);
    let state = reducePool(req.state, { type: 'rest', token: detail.token, epoch: 'e1', rows: [puppet()] });
    state = event(state, '1', {}, 'creation-after-snapshot');
    state = reducePool(state, { type: 'rest', token: req.token, epoch: 'e1', rows: [],
      page: { snapshotId: 's', cursor: 'c', next: null } });
    expect(state.collections.puppet.ids).toContain(key);
    expect(state.entities[key].value?.revision).toBe('1');
    expect(state.entities[key].invalidated).toBe(false);
  });

  it('protects an equal-version detail read issued after the snapshot started', () => {
    const req = snapshot(read(initial(), [puppet()]));
    let state = read(req.state, [puppet()]);
    state = reducePool(state, { type: 'rest', token: req.token, epoch: 'e1', rows: [], page: { snapshotId: 's', cursor: 'c', next: null } });
    expect(state.collections.puppet.ids).toContain(key);
  });

  it('does not reintroduce list membership from detail issued before the new snapshot', () => {
    const old = beginRead(initial(), { type: 'detail' });
    const req = snapshot(old.state);
    let state = reducePool(req.state, { type: 'rest', token: req.token, epoch: 'e1', rows: [], page: { snapshotId: 's', cursor: 'c', next: null } });
    state = reducePool(state, { type: 'rest', token: old.token, epoch: 'e1', rows: [puppet()] });
    expect(state.collections.puppet.ids).toEqual([]);
    state = read(state, [puppet()]);
    expect(state.collections.puppet.ids).toContain(key);
  });

  it('binds pagination to snapshot identity and exact opaque continuation, not cursor order', () => {
    const req = snapshot(initial());
    let state = reducePool(req.state, { type: 'rest', token: req.token, epoch: 'e1', rows: [puppet()], page: { snapshotId: 's1', cursor: 'z', next: 'a' } });
    const wrong = beginRead(state, { type: 'snapshot', kind: 'puppet', snapshotId: 's1', cursor: 'z' });
    state = reducePool(wrong.state, { type: 'rest', token: wrong.token, epoch: 'e1', rows: [puppet('1', 'i1', 'wrong')], page: { snapshotId: 's1', cursor: 'z', next: null } });
    expect(state.entities[entityKey(puppet('1', 'i1', 'wrong'))]).toBeUndefined();
    const next = beginRead(state, { type: 'snapshot', kind: 'puppet', snapshotId: 's1', cursor: 'a' });
    state = reducePool(next.state, { type: 'rest', token: next.token, epoch: 'e1', rows: [puppet('1', 'i1', 'p2')], page: { snapshotId: 's1', cursor: 'z', next: null } });
    expect(state.collections.puppet.complete).toBe(true);
    expect(state.collections.puppet.ids).toHaveLength(2);
  });

  it('rejects pages from a superseded snapshot and mixed server snapshot identities', () => {
    const old = snapshot(initial());
    const fresh = snapshot(old.state);
    let state = reducePool(fresh.state, { type: 'rest', token: old.token, epoch: 'e1', rows: [puppet('9')], page: { snapshotId: 'old', cursor: 'a', next: null } });
    expect(state.entities).toEqual({});
    state = reducePool(state, { type: 'rest', token: fresh.token, epoch: 'e1', rows: [puppet()], page: { snapshotId: 's1', cursor: 'z', next: 'a' } });
    const next = beginRead(state, { type: 'snapshot', kind: 'puppet', snapshotId: 's1', cursor: 'a' });
    state = reducePool(next.state, { type: 'rest', token: next.token, epoch: 'e1', rows: [puppet('9')], page: { snapshotId: 's2', cursor: 'z', next: null } });
    expect(state.resync).toBe('snapshot_mismatch');
    expect(state.entities).toEqual({});
  });
});
