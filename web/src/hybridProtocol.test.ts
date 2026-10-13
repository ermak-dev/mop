// HYPOTHESIS: снимки истории нельзя склеивать через эпоху, а ввод нельзя
// резать по символам: UTF-8 и управляющие байты принадлежат терминалу.
// SOLUTION: редуктор эпох, честный разбор записей, порции байтов до 4096.
// RESULT: RED до реализации; STATUS: FIXED — see #601
import { expect, test } from 'vitest';
import { chatEntries, emptyHistory, encodeInput, humanInput, mergeHistory, STOP } from './hybridProtocol';
import type { HistoryPage } from './hybridProtocol';
const page = (changes: Partial<HistoryPage> = {}): HistoryPage => ({ epoch: 'e1', session_id: 's1', cursor: 'opaque1', records: [{ uuid: 'u', type: 'user', message: { content: 'hello' } }], reset: false, has_more: false, partial: false, ...changes });
const decode = (parts: string[]) => new TextDecoder().decode(Uint8Array.from(parts.flatMap(p => Array.from(atob(p), c => c.charCodeAt(0)))));
test('history replaces on epoch/session/reset and deduplicates only stable ids within scope', () => {
  const first = mergeHistory(emptyHistory, page());
  expect(first.records).toHaveLength(1);
  expect(mergeHistory(first, page())).toEqual(first);
  const next = mergeHistory(first, page({ cursor: 'opaque2', records: [{ uuid: 'u', type: 'user', message: { content: 'updated' } }, { type: 'system' }] }));
  expect(next.records).toHaveLength(2);
  expect(next.records[0].message).toEqual({ content: 'updated' });
  for (const change of [{ epoch: 'e2' }, { session_id: 's2' }, { reset: true }]) {
    const reset = mergeHistory(next, page({ ...change, cursor: 'new', records: [{ type: 'system' }], partial: true }));
    expect(reset.records).toEqual([{ type: 'system' }]);
    expect(reset.partial).toBe(true);
  }
});
test('Claude text/tool records preserve origin, correlated result errors and unknown metadata', () => {
  const entries = chatEntries([
    { uuid: 'a', type: 'assistant', message: { content: [{ type: 'text', text: '<img src=x> **safe**' }, { type: 'tool_use', id: 't1', name: 'Bash', input: { command: 'pwd' } }] } },
    { uuid: 'b', type: 'user', origin: 'peer', message: { content: [{ type: 'tool_result', tool_use_id: 't1', content: 'denied', is_error: true }, { type: 'text', text: 'peer instruction' }] } },
    { type: 'user', message: { content: 'no proven origin' } },
    { type: 'system', subtype: 'compact_boundary', compactMetadata: { trigger: 'auto' } },
    { type: 'future_record', secret: 'not dropped' },
  ]);
  expect(entries.find(e => e.kind === 'text')?.text).toBe('<img src=x> **safe**');
  expect(entries.find(e => e.kind === 'tool')).toMatchObject({ id: 't1', name: 'Bash', result: 'denied', error: true });
  expect(entries.find(e => e.text === 'peer instruction')?.origin).toBe('peer');
  expect(entries.find(e => e.text === 'no proven origin')?.origin).toBe('unknown');
  expect(entries.some(e => e.kind === 'compaction')).toBe(true);
  expect(entries.find(e => e.kind === 'unknown')?.raw).toEqual({ type: 'future_record', secret: 'not dropped' });
});
test('orphan results, partial messages and non-text blocks remain visible not fabricated completions', () => {
  const entries = chatEntries([{ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'missing', content: 'orphan' }, { type: 'image', source: { url: 'https://remote.test/image' } }] } }, { type: 'assistant', isPartial: true, message: { content: 'part' } }]);
  expect(entries.find(e => e.id === 'missing')?.kind).toBe('result');
  expect(entries.filter(e => e.kind === 'unknown')).toHaveLength(1);
  expect(entries.some(e => e.kind === 'meta')).toBe(true);
});
test('human path is bracketed UTF-8 paste plus Enter, slash identical and Stop Escape only', () => {
  expect(decode(humanInput('Привет 𝄞'))).toBe('\x1b[200~Привет 𝄞\x1b[201~\r');
  expect(decode(humanInput('/model'))).toBe('\x1b[200~/model\x1b[201~\r');
  expect(decode(humanInput('next', true))).toBe('\x1b\x1b[200~next\x1b[201~\r');
  expect(STOP).toBe('\x1b');
});
test('string messages and correlated tool result metadata remain honest and inspectable', () => {
  const result = { type: 'user', origin: 'human', timestamp: 'now', message: { content: [{ type: 'tool_result', tool_use_id: 't', content: 'ok' }] } };
  const entries = chatEntries([{ type: 'user', message: 'direct string' }, { type: 'assistant', message: { content: [{ type: 'tool_use', id: 't', name: 'Read', input: {} }] } }, result]);
  expect(entries[0]).toMatchObject({ kind: 'text', text: 'direct string', origin: 'unknown' });
  expect(entries.find(e => e.kind === 'tool')).toMatchObject({ resultOrigin: 'human', resultRaw: result });
});
test('composer text cannot escape bracketed paste or inject terminal controls', () => {
  expect(() => humanInput('text\x1b[201~injected\r')).toThrow(/control/i);
  expect(() => humanInput('text\x03')).toThrow(/control/i);
  expect(decode(humanInput('line1\nline2\tcolumn'))).toBe('\x1b[200~line1\nline2\tcolumn\x1b[201~\r');
});
test('large input chunks concatenate exact bytes and never exceed wire limit', () => {
  const text = '界'.repeat(5000);
  const parts = encodeInput(text);
  expect(parts.length).toBeGreaterThan(1);
  expect(parts.every(p => atob(p).length <= 4096)).toBe(true);
  expect(decode(parts)).toBe(text);
});
