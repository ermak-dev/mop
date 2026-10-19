// #627: новая цель -- create intent проекта через общий store, не старый add.
// HYPOTHESIS: главная страница ещё показывает заглушку вместо работающей формы.
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { CreateBox } from './CreateBox';
import { renderUi } from '../test-utils';
import { ADMIN, PROJECT, networkFixture, reply } from '../network-test-utils';

const NODE = '00000000-0000-4000-8000-000000000006';
const DEFINITION = '00000000-0000-4000-8000-000000000007';
let net: ReturnType<typeof networkFixture>;
beforeEach(() => { net = networkFixture(); });
afterEach(() => { net.stop(); vi.unstubAllGlobals(); });
const open = async () => {
  await net.start();
  renderUi(<CreateBox session={ADMIN} />);
  fireEvent.click(screen.getByRole('button', { name: 'Завести папета' }));
  for (const [name, value] of [['ID проекта', PROJECT], ['ID узла', NODE], ['ID одобренного определения', DEFINITION]])
    fireEvent.change(await screen.findByRole('textbox', { name }), { target: { value } });
};

test('#627 form sends exactly the project create intent once', async () => {
  await open();
  const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => {
    if (init?.method === 'POST') { net.capture(init); return Promise.resolve(reply(net.operation('accepted', '1'), 202)); }
    return base(url, init);
  });
  const submit = screen.getByRole('button', { name: 'Создать' });
  fireEvent.click(submit); fireEvent.click(submit);
  await waitFor(() => expect(net.posts()).toHaveLength(1));
  expect(net.posts()[0][0]).toBe(`/api/v1/projects/${PROJECT}/commands/create-puppet`);
  expect(JSON.parse(String(net.posts()[0][1]?.body)).params).toEqual({ node_id: NODE, definition_id: DEFINITION });
  await waitFor(() => expect(screen.getByRole('textbox', { name: 'ID проекта' })).toBeDisabled());
  expect(screen.getByRole('button', { name: 'Создать' })).toBeDisabled();
});

test('#627 an unknown create stays locked and offers GET recovery, not another create', async () => {
  await open();
  const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => {
    if (init?.method === 'POST') { net.capture(init); return Promise.reject(new Error('offline')); }
    if (String(url).startsWith('/api/v1/operations')) return Promise.resolve(reply({ schema_version: 1,
      error: { code: 'not_found', message: 'not found', retryable: false } }, 404));
    return base(url, init);
  });
  fireEvent.click(screen.getByRole('button', { name: 'Создать' }));
  await screen.findByText(/Исход неизвестен/);
  expect(screen.getByRole('button', { name: 'Создать' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'Проверить исход' }));
  await waitFor(() => expect(net.http.mock.calls.filter(([url]) => String(url).startsWith('/api/v1/operations')).length).toBeGreaterThan(1));
  expect(net.posts()).toHaveLength(1);
});

test('#627 guests have no create entry', () => {
  renderUi(<CreateBox session={{ login: null, role: null }} />);
  expect(screen.queryByRole('button', { name: 'Завести папета' })).toBeNull();
});
