import { useEffect, useLayoutEffect, useRef } from 'react';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import { clipboardInput, encodeInput } from '../hybridProtocol';
import './hybridWorkspace.css';

export function HybridTerminal({ visible, writable, bind, send, resize }: {
  visible: boolean; writable: boolean; bind: (terminal: Terminal | null) => void;
  send: (parts: string[]) => Promise<boolean>; resize: (cols: number, rows: number) => Promise<void>;
}) {
  const container = useRef<HTMLDivElement>(null);
  const terminal = useRef<Terminal | null>(null);
  const fit = useRef<FitAddon | null>(null);
  const actions = useRef({ send, resize, writable }); actions.current = { send, resize, writable };
  useLayoutEffect(() => {
    const term = new Terminal({
      disableStdin: true, cursorBlink: false, screenReaderMode: true,
      fontFamily: 'var(--mantine-font-family-monospace)', fontSize: 13, lineHeight: 1.25,
      scrollback: 5000, allowProposedApi: false, windowOptions: {},
      linkHandler: { activate: () => { /* Терминальные URI не открывают браузер. */ } },
      theme: { background: '#1a1b1e', foreground: '#f1f3f5', cursor: '#74c0fc' },
    });
    const addon = new FitAddon(); term.loadAddon(addon);
    const element = container.current!;
    const paste = (event: ClipboardEvent) => {
      // Capture до xterm: clipboard не равен намеренным keyboard controls.
      event.preventDefault(); event.stopImmediatePropagation();
      if (!actions.current.writable || !event.clipboardData) return;
      const text = event.clipboardData.getData('text/plain');
      if (!text) return;
      try { void actions.current.send(clipboardInput(text)); }
      catch { element.setAttribute('aria-description', 'Вставка отклонена: управляющие символы недопустимы.'); }
    };
    element.addEventListener('paste', paste, true);
    term.open(element);
    terminal.current = term; fit.current = addon; bind(term);
    // OSC 52 не получает доступ к clipboard; windowOptions остаются запрещены.
    const clipboard = term.parser.registerOscHandler(52, () => true);
    const input = term.onData(data => { if (actions.current.writable) void actions.current.send(encodeInput(data)); });
    const binary = term.onBinary(data => { if (actions.current.writable) void actions.current.send([btoa(data)]); });
    const size = term.onResize(({ cols, rows }) => { if (actions.current.writable) void actions.current.resize(cols, rows); });
    const observer = new ResizeObserver(() => { if (container.current?.clientWidth) addon.fit(); });
    observer.observe(container.current!);
    return () => {
      element.removeEventListener('paste', paste, true);
      observer.disconnect(); input.dispose(); binary.dispose(); size.dispose(); clipboard.dispose();
      bind(null); terminal.current = null; fit.current = null; term.dispose();
    };
  }, [bind]);
  useEffect(() => {
    if (terminal.current) terminal.current.options.disableStdin = !writable;
    if (visible && container.current?.clientWidth) fit.current?.fit();
  }, [visible, writable]);
  return <div ref={container} className="hybrid-terminal" role="region" aria-label={writable ? 'Терминал: управление' : 'Терминал: только чтение'} />;
}
