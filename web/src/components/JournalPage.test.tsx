// HYPOTHESIS: bus table/full_entry семантика неверна для immutable operation evidence.
// SPEC: server order, actual metadata/IDs, selection-only state, accessible dialog.
// SOLUTION: таблица показывает audit metadata, selection хранит только ID/source.
// RESULT: RED → GREEN. STATUS: FIXED — see #627
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { expect, test, vi } from 'vitest';
import { MantineProvider } from '@mantine/core';
import { renderUi } from '../test-utils';
import { JournalPage } from './JournalPage';
import { auditItem } from '../audit-test-utils';
import { ID, OP } from '../network-test-utils';
const J = [auditItem('20', 'newest refusal'), auditItem('19', 'older refusal')];

test('renders truthful audit coverage and server ordering, with actual target ids and safe reason', () => {
  renderUi(<JournalPage journal={J} sourceKey="source" />);
  expect(screen.getByRole('heading', { name: 'Operation audit' })).toBeInTheDocument();
  const rows = screen.getAllByRole('row').slice(1);
  expect(rows[0].textContent).toContain('newest refusal'); expect(rows[1].textContent).toContain('older refusal');
  expect(rows[0].textContent).toContain(ID); expect(rows[0].textContent).toContain('dispatch');
  expect(rows[0].textContent).toContain('refused'); expect(rows[0].textContent).toContain('dispatch_declined');
  expect(rows[0].textContent).toContain('18.10.2026 10:05:00 UTC');
  expect(screen.queryByText(/дождался простоя|сообщение от/)).toBeNull();
});

test('keyboard-focusable evidence button and row click open exact evidence, Escape closes', async () => {
  renderUi(<JournalPage journal={J} sourceKey="source" />);
  const button = screen.getByRole('button', { name: 'Open audit evidence 20' });
  button.focus(); expect(button).toHaveFocus(); fireEvent.click(button);
  const dialog = await screen.findByRole('dialog');
  expect(within(dialog).getByText(OP)).toBeInTheDocument();
  fireEvent.keyDown(dialog, { key: 'Escape' });
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  fireEvent.click(screen.getByText('older refusal'));
  expect(await screen.findByRole('dialog')).toHaveTextContent('older refusal');
});

test('empty and unavailable are distinct; unavailable never leaks stale rows or selected metadata', async () => {
  const view = renderUi(<JournalPage journal={J} sourceKey="source" />);
  fireEvent.click(screen.getByRole('button', { name: 'Open audit evidence 20' }));
  expect(await screen.findByRole('dialog')).toBeInTheDocument();
  view.rerender(<MantineProvider><JournalPage journal={J} error="Operation audit unavailable: permission denied" sourceKey="source" /></MantineProvider>);
  expect(screen.queryByRole('dialog')).toBeNull(); expect(screen.queryByRole('row')).toBeNull();
  expect(screen.getByRole('alert')).toHaveTextContent('permission denied');
  view.rerender(<MantineProvider><JournalPage journal={[]} sourceKey="source" /></MantineProvider>);
  expect(screen.getByText('No operation audit evidence in the current scope')).toBeInTheDocument();
  expect(screen.queryByRole('dialog')).toBeNull();
});

test('same-source poll preserves selection but source/epoch/scope switch closes it immediately', async () => {
  const view = renderUi(<JournalPage journal={J} sourceKey="source" />);
  fireEvent.click(screen.getByRole('button', { name: 'Open audit evidence 20' }));
  view.rerender(<MantineProvider><JournalPage journal={[...J]} sourceKey="source" /></MantineProvider>);
  expect(await screen.findByRole('dialog')).toBeInTheDocument();
  view.rerender(<MantineProvider><JournalPage journal={J} sourceKey="new-source" /></MantineProvider>);
  expect(screen.queryByRole('dialog')).toBeNull();
});

test('selection is only an id: a removed row cannot leave copied DTO in the modal', async () => {
  const view = renderUi(<JournalPage journal={J} sourceKey="source" />);
  fireEvent.click(screen.getByRole('button', { name: 'Open audit evidence 20' }));
  expect(await screen.findByRole('dialog')).toBeInTheDocument();
  view.rerender(<MantineProvider><JournalPage journal={[J[1]]} sourceKey="source" /></MantineProvider>);
  expect(screen.queryByRole('dialog')).toBeNull();
});

test('pagination controls are accessible, bounded by owner and disabled while a page is loading', () => {
  const more = vi.fn(), refresh = vi.fn();
  const view = renderUi(<JournalPage journal={J} sourceKey="source" nextCursor="opaque" more={more} refresh={refresh} />);
  fireEvent.click(screen.getByRole('button', { name: 'Load older evidence' })); expect(more).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByRole('button', { name: 'Refresh operation audit' })); expect(refresh).toHaveBeenCalledOnce();
  view.rerender(<MantineProvider><JournalPage journal={J} sourceKey="source" loading nextCursor="opaque" more={more} /></MantineProvider>);
  expect(screen.getByRole('button', { name: 'Load older evidence' })).toBeDisabled();
});
