// HYPOTHESIS: заголовок дублируется, а ключ цели сбрасывает настройки панели.
// SOLUTION: единая постоянная панель; движки операций вне приватного тела.
// RESULT: до реализации проверки падают на старом заголовке и потере вкладки.
// STATUS: FIXED — see #666
import { useEffect } from 'react';
import { MantineProvider } from '@mantine/core';
import { fireEvent, screen, within } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { renderUi } from '../test-utils';
import type { Puppet } from '../types';
import { PuppetCard } from './PuppetCard';

const engines = vi.hoisted(() => ({ mounted: vi.fn(), unmounted: vi.fn() }));
vi.mock('./PuppetActions', () => ({ PuppetActions: ({ row }: { row: Puppet }) => {
  useEffect(() => { engines.mounted(row.name); return () => { engines.unmounted(row.name); }; }, [row.name]);
  return <button aria-label="Правка">edit</button>;
} }));
const admin = { login: 'anton', role: 'admin' as const };
const rows = [{ name: 'p' }, { name: 'q' }] as Puppet[];
const card = (target: string | null) => <PuppetCard session={admin} rows={rows} target={target} role={target === 'p' ? 'builder' : 'reviewer'} onClose={() => {}} />;
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks(); });
function mount() {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => ({ ok: true, status: 200, json: async () => url.includes('/history')
    ? { epoch: 'e', session_id: 's', cursor: 'c', records: [], reset: false, partial: false, has_more: false }
    : { source: 'initialization', lines: [], truncated: false } })));
  return renderUi(card('p'));
}

test('#666 one compact toolbar orders tabs, identity, edit, metadata and close', async () => {
  mount();
  const aside = screen.getByRole('complementary', { name: 'экран p' });
  const toolbar = within(aside).getByRole('toolbar');
  expect(within(toolbar).getAllByRole('tab').map(tab => tab.textContent)).toEqual(['Чат', 'Терминал']);
  expect(within(toolbar).getByText('p (builder)')).toBeVisible();
  expect(within(aside).queryByText('экран p')).toBeNull();
  const ordered = [within(toolbar).getByRole('tab', { name: 'Терминал' }), within(toolbar).getByText('p (builder)'),
    within(toolbar).getByRole('button', { name: 'Правка' }), within(toolbar).getByRole('checkbox'),
    within(toolbar).getByRole('button', { name: 'Закрыть' })];
  ordered.slice(1).forEach((node, index) => expect(ordered[index].compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy());
  await screen.findByRole('textbox');
});

test('#666 selection and closing preserve panel preferences and every canonical operation engine', async () => {
  const view = mount();
  await screen.findByRole('textbox');
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(screen.getByRole('tab', { name: 'Терминал' }));
  for (const target of ['q', null, 'p']) {
    view.rerender(<MantineProvider>{card(target)}</MantineProvider>);
    expect(engines.mounted.mock.calls.map(([name]) => name)).toEqual(['p', 'q']);
    expect(engines.unmounted).not.toHaveBeenCalled();
    if (target) expect(screen.getByRole('tab', { name: 'Терминал' })).toHaveAttribute('aria-selected', 'true');
    else expect(screen.queryByLabelText('Ввод человека в CLI')).toBeNull();
  }
  fireEvent.click(screen.getByRole('tab', { name: 'Чат' }));
  expect(screen.getByRole('checkbox')).toBeChecked();
  expect(screen.getByRole('textbox')).toHaveValue('');
  view.rerender(<MantineProvider>{card('q')}</MantineProvider>);
  expect(screen.getByRole('tab', { name: 'Чат' })).toHaveAttribute('aria-selected', 'true');
  expect(screen.getByRole('checkbox')).toBeChecked();
  expect(engines.unmounted).not.toHaveBeenCalled();
});
