// HYPOTHESIS: фиксированное соотношение панелей не даёт выбрать ширину экрана.
// SOLUTION: ширина ростера живёт в постоянной обёртке; capture ограничен разделителем.
// RESULT: семь поведенческих проверок упали на старой обёртке, затем все восемь прошли.
// Нулевой размер первого layout воспроизведён отдельным красным прогоном.
// STATUS: FIXED — see #666
import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { useState } from 'react';
import { DashboardSplit } from './DashboardSplit';

const main = <main className="dashboard-main"><input aria-label="roster draft" defaultValue="keep" /></main>;
const panel = (name = 'one') => <aside className="dashboard-screen">{name}</aside>;
const split = (open = true, name = 'one') => <DashboardSplit open={open} main={main} screen={panel(name)} />;
const separator = () => screen.getByRole('separator', { name: 'Resize roster and screen' });
const value = () => Number(separator().getAttribute('aria-valuenow'));
const resize = (width: number) => act(() => {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: width });
  window.dispatchEvent(new Event('resize'));
});
beforeEach(() => {
  resize(1400);
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(() => ({
    x: 12, y: 0, left: 12, top: 0, right: window.innerWidth - 12, bottom: 600,
    width: window.innerWidth - 24, height: 600, toJSON() {},
  }));
  // jsdom не реализует PointerEvent и захват; сохраняем координаты и id указателя.
  vi.stubGlobal('PointerEvent', class extends MouseEvent {
    pointerId: number;
    constructor(type: string, init: PointerEventInit = {}) { super(type, init); this.pointerId = init.pointerId ?? 7; }
  });
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

test('#666 default split reserves exactly the first three roster columns', () => {
  render(split());
  expect(value()).toBe(378);
  expect(separator()).toHaveAttribute('aria-orientation', 'vertical');
  expect(separator()).toHaveAttribute('tabindex', '0');
  expect(separator()).toHaveAttribute('aria-valuemin', '260');
  expect(separator()).toHaveAttribute('aria-valuemax', String(1400 - 24 - 12 - 440));
  expect(document.querySelector('.dashboard-body')).toHaveStyle({ '--roster-width': '378px' });
});

test('#666 keyboard changes and clamps the roster width, ignoring unrelated keys', () => {
  render(split());
  separator().focus();
  fireEvent.keyDown(separator(), { key: 'ArrowRight' });
  expect(value()).toBe(398);
  fireEvent.keyDown(separator(), { key: 'ArrowLeft' });
  expect(value()).toBe(378);
  fireEvent.keyDown(separator(), { key: 'Home' });
  expect(value()).toBe(260);
  fireEvent.keyDown(separator(), { key: 'ArrowLeft' });
  expect(value()).toBe(260);
  fireEvent.keyDown(separator(), { key: 'End' });
  expect(value()).toBe(924);
  fireEvent.keyDown(separator(), { key: 'ArrowRight' });
  fireEvent.keyDown(separator(), { key: 'Enter' });
  expect(value()).toBe(924);
});

test('#666 pointer capture drags relative to the starting width and stops on release or cancel', () => {
  render(split());
  const handle = separator();
  const capture = vi.fn();
  const release = vi.fn();
  Object.assign(handle, { setPointerCapture: capture, releasePointerCapture: release, hasPointerCapture: () => true });
  fireEvent.pointerMove(handle, { clientX: 600 });
  expect(value()).toBe(378);
  fireEvent.pointerDown(handle, { clientX: 390, button: 2 });
  expect(capture).not.toHaveBeenCalled();
  fireEvent.pointerDown(handle, { clientX: 390, button: 0 });
  expect(capture).toHaveBeenCalledTimes(1);
  fireEvent.pointerMove(handle, { clientX: 490 });
  expect(value()).toBe(478);
  fireEvent.pointerMove(handle, { clientX: -100 });
  expect(value()).toBe(260);
  fireEvent.pointerMove(handle, { clientX: 4000 });
  expect(value()).toBe(924);
  fireEvent.pointerUp(handle);
  expect(release).toHaveBeenCalledTimes(1);
  fireEvent.pointerMove(handle, { clientX: 400 });
  expect(value()).toBe(924);
  fireEvent.pointerDown(handle, { clientX: 940, button: 0 });
  fireEvent.pointerCancel(handle);
  fireEvent.pointerMove(handle, { clientX: 400 });
  expect(value()).toBe(924);
});

test('#666 lost capture ends drag without document listeners', () => {
  const listen = vi.spyOn(document, 'addEventListener');
  render(split());
  const handle = separator();
  Object.assign(handle, { setPointerCapture: vi.fn() });
  fireEvent.pointerDown(handle, { clientX: 390, button: 0 });
  fireEvent.lostPointerCapture(handle);
  fireEvent.pointerMove(handle, { clientX: 600 });
  expect(value()).toBe(378);
  expect(listen.mock.calls.filter(([type]) => /pointer|mouse|touch/.test(type))).toEqual([]);
});

test('#666 resizing clamps to preserve the screen minimum, mobile has no divider', () => {
  render(split());
  fireEvent.keyDown(separator(), { key: 'End' });
  resize(1000);
  expect(value()).toBe(524);
  expect(separator()).toHaveAttribute('aria-valuemax', '524');
  resize(901);
  expect(value()).toBe(425);
  resize(900);
  expect(screen.queryByRole('separator')).toBeNull();
  resize(390);
  expect(screen.queryByRole('separator')).toBeNull();
  resize(1400);
  expect(value()).toBe(924);
});

test('#666 close/reopen and target switch keep the split and mounted children', () => {
  function Draft() { const [text, setText] = useState('keep'); return <input aria-label="screen draft" value={text} onChange={e => setText(e.target.value)} />; }
  const children = <Draft />;
  const { rerender } = render(<DashboardSplit open main={main} screen={children} />);
  fireEvent.keyDown(separator(), { key: 'ArrowRight' });
  const roster = screen.getByRole('textbox', { name: 'roster draft' });
  const draft = screen.getByRole('textbox', { name: 'screen draft' });
  fireEvent.change(draft, { target: { value: 'unsent' } });
  rerender(<DashboardSplit open={false} main={main} screen={children} />);
  expect(screen.queryByRole('separator')).toBeNull();
  expect(screen.getByRole('textbox', { name: 'screen draft' })).toBe(draft);
  rerender(<DashboardSplit open main={main} screen={children} />);
  expect(value()).toBe(398);
  expect(screen.getByRole('textbox', { name: 'screen draft' })).toHaveValue('unsent');
  expect(screen.getByRole('textbox', { name: 'roster draft' })).toBe(roster);
  rerender(split(true, 'two'));
  expect(value()).toBe(398);
});

test('#666 an initially unmeasured container uses viewport bounds until layout is available', () => {
  vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockReturnValue({ x: 0, y: 0, left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, toJSON() {} });
  render(split());
  expect(value()).toBe(378);
  fireEvent.keyDown(separator(), { key: 'ArrowRight' });
  expect(value()).toBe(398);
});

test('#666 unmount removes its resize subscription', () => {
  const add = vi.spyOn(window, 'addEventListener');
  const remove = vi.spyOn(window, 'removeEventListener');
  const { unmount } = render(split());
  const callbacks = add.mock.calls.filter(([type]) => type === 'resize').map(([, callback]) => callback);
  expect(callbacks.length).toBeGreaterThan(0);
  unmount();
  for (const callback of callbacks) expect(remove).toHaveBeenCalledWith('resize', callback);
});
