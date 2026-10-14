// HYPOTHESIS: xterm onData смешивает clipboard с намеренными клавишами.
// SOLUTION: capture paste до xterm; безопасная bracketed paste без Enter.
// STATUS: FIXED — see #601
import { fireEvent, screen } from '@testing-library/react';
import { expect, test, vi } from 'vitest';
import { renderUi } from '../test-utils';
import { HybridTerminal } from './HybridTerminal';
const native = vi.hoisted(() => ({ data: (_value: string) => {} }));
vi.mock('@xterm/xterm', () => ({ Terminal: class {
  options = {}; parser = { registerOscHandler: () => ({ dispose() {} }) };
  loadAddon() {} open() {} dispose() {}
  onData(callback: (value: string) => void) { native.data = callback; return { dispose() {} }; }
  onBinary() { return { dispose() {} }; } onResize() { return { dispose() {} }; }
} }));
vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit() {} } }));
test('clipboard capture rejects escaped premature bracket end and CR; native keyboard controls remain available', () => {
  const send = vi.fn(async (_parts: string[]) => true);
  renderUi(<HybridTerminal visible writable bind={() => {}} send={send} resize={async () => {}} />);
  const element = screen.getByRole('region');
  const event = new Event('paste', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'clipboardData', { value: { getData: () => '\x1b[201~danger\r' } });
  element.dispatchEvent(event);
  expect(event.defaultPrevented).toBe(true);
  expect(send).not.toHaveBeenCalled();
  native.data('\x03');
  expect(send).toHaveBeenCalledWith(['Aw==']);
});
test('normal multiline UTF8 clipboard is bracketed without automatic Enter; ro cannot paste', () => {
  const send = vi.fn(async (_parts: string[]) => true);
  const bind = () => {};
  const view = renderUi(<HybridTerminal visible writable bind={bind} send={send} resize={async () => {}} />);
  const paste = () => fireEvent.paste(screen.getByRole('region'), { clipboardData: { getData: () => 'hello\nмир\t!' } });
  paste();
  const bytes = Uint8Array.from(atob(send.mock.calls[0][0].join('')), c => c.charCodeAt(0));
  expect(new TextDecoder().decode(bytes)).toBe('\x1b[200~hello\nмир\t!\x1b[201~');
  send.mockClear();
  view.rerender(<HybridTerminal visible writable={false} bind={bind} send={send} resize={async () => {}} />);
  paste(); expect(send).not.toHaveBeenCalled();
});
