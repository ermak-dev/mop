// HYPOTHESIS (#627): create API уже существует, общий store не умеет
// принимать новую цель без incarnation/revision и GUI остаётся заглушкой.
// SPEC: только проект/узел/approved definition; epoch из общего snapshot,
// request key фиксирован, неизвестный POST восстанавливается только GET.
// SOLUTION: project create в общем runtime и reducer, без target binding.
// RESULT: RED -- метода не было; GREEN -- web suite, types и dist --check.
// STATUS: FIXED — see #627
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { poolNetwork } from './poolNetwork';
import { EPOCH, ID, PROJECT, networkFixture, reply } from './network-test-utils';

const NODE = '00000000-0000-4000-8000-000000000006';
const DEFINITION = '00000000-0000-4000-8000-000000000007';
let requestId: string;
let net: ReturnType<typeof networkFixture>;
beforeEach(() => { net = networkFixture(); requestId = crypto.randomUUID(); });
afterEach(() => { net.stop(); vi.unstubAllGlobals(); });

const acceptCreate = () => {
  const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => {
    if (url === `/api/v1/projects/${PROJECT}/commands/create-puppet` && init?.method === 'POST') {
      net.capture(init);
      return Promise.resolve(reply(net.operation('accepted', '1'), 202));
    }
    return base(url, init);
  });
};

test('#627 create uses project admission without a fabricated target binding', async () => {
  await net.start(); acceptCreate();
  await poolNetwork.create(PROJECT, NODE, DEFINITION, requestId);
  expect(net.posts()).toHaveLength(1);
  const [url, init] = net.posts()[0];
  expect(url).toBe(`/api/v1/projects/${PROJECT}/commands/create-puppet`);
  expect(JSON.parse(String(init?.body))).toEqual({ schema_version: 1, epoch: EPOCH, request_id: requestId,
    not_after: expect.any(String), params: { node_id: NODE, definition_id: DEFINITION } });
  expect(poolNetwork.operation(requestId)?.operation?.target.id).toBe(ID);
});

test('#627 lost create reply recovers the same key by GET, never by a second POST', async () => {
  await net.start();
  const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => {
    if (init?.method === 'POST') { net.capture(init); return Promise.reject(new Error('offline')); }
    if (String(url).startsWith('/api/v1/operations')) return Promise.resolve(reply(net.operation('running')));
    return base(url, init);
  });
  await poolNetwork.create(PROJECT, NODE, DEFINITION, requestId);
  expect(net.posts()).toHaveLength(1);
  expect(net.http.mock.calls.some(([url]) => String(url).includes(`request_id=${requestId}`))).toBe(true);
  expect(poolNetwork.operation(requestId)?.operation?.requestId).toBe(requestId);
});

test('#627 a tracked create key is recovered rather than reposted', async () => {
  await net.start(); acceptCreate();
  await poolNetwork.create(PROJECT, NODE, DEFINITION, requestId);
  await expect(poolNetwork.create(PROJECT, NODE, DEFINITION, requestId)).rejects.toThrow(/already tracked/);
  expect(net.posts()).toHaveLength(1);
});

test.each(['project', 'node', 'definition', 'request'])('#627 invalid %s id refuses before any POST', async field => {
  await net.start();
  const values = [PROJECT, NODE, DEFINITION, requestId];
  values[['project', 'node', 'definition', 'request'].indexOf(field)] = '../other';
  await expect(poolNetwork.create(...values as [string, string, string, string])).rejects.toThrow();
  expect(net.posts()).toHaveLength(0);
});
