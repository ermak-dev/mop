// HYPOTHESIS: arbitrary full_entry раскрывает поля вне frozen audit whitelist.
// SPEC: только actual operation metadata; никаких actor/name/private payload guesses.
// SOLUTION: диалог и clipboard используют только frozen audit whitelist.
// RESULT: RED → GREEN. STATUS: FIXED — see #627
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { expect, test, vi } from 'vitest';
import { renderUi } from '../test-utils';
import { JournalModal } from './JournalModal';
import { auditItem } from '../audit-test-utils';
import { ID, INCARNATION, OP, PROJECT } from '../network-test-utils';

test('evidence dialog shows the exact frozen metadata without bus interpretations', () => {
  renderUi(<JournalModal entry={auditItem()} onClose={() => {}} />);
  const dialog = screen.getByRole('dialog');
  for (const value of [ID, OP, INCARNATION, PROJECT, '9007199254740995', '2026-10-18T10:05:00Z', 'dispatch_declined', 'target does not receive dispatch']) expect(dialog.textContent).toContain(value);
  expect(dialog).toHaveAttribute('aria-label', 'Operation audit evidence 9007199254740993');
  expect(dialog.textContent).not.toMatch(/from |сообщение|actor:|full_entry|payload/);
});

test('long safe reason wraps instead of overflowing; null incarnation is not a fake identity', () => {
  const entry = { ...auditItem(), reason: 'long safe refusal '.repeat(100).trim(), target: { kind: 'node' as const, id: ID, incarnation: null } };
  renderUi(<JournalModal entry={entry} onClose={() => {}} />);
  expect(screen.getByRole('dialog').textContent).not.toContain('undefined');
  expect(screen.getByText(entry.reason).closest('[style]')?.getAttribute('style')).not.toContain('nowrap');
});

test('Escape and the accessible close button dismiss evidence', () => {
  const close = vi.fn(); renderUi(<JournalModal entry={auditItem()} onClose={close} />);
  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' }); expect(close).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('button', { name: 'Close audit evidence' })); expect(close).toHaveBeenCalledTimes(2);
});

test('copies only frozen metadata, no arbitrary fields even if a caller adds them', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal('navigator', { clipboard: { writeText } });
  const entry = { ...auditItem(), payload: { message: 'private' }, full_entry: { secret: 'private' } };
  renderUi(<JournalModal entry={entry} onClose={() => {}} />);
  expect(screen.getByRole('dialog').textContent).not.toContain('private');
  fireEvent.click(screen.getByRole('button', { name: 'Copy audit evidence' }));
  await waitFor(() => expect(writeText).toHaveBeenCalledOnce());
  expect(JSON.parse(writeText.mock.calls[0][0])).toEqual(auditItem());
  vi.unstubAllGlobals();
});
