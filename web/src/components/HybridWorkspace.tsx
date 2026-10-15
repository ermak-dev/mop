import { useCallback, useEffect, useMemo, useRef, useState, useId } from 'react';
import type { ReactNode } from 'react';
import type { Terminal } from '@xterm/xterm';
import { Box, Button, Checkbox, Tabs, Text, Textarea } from '@mantine/core';
import type { Session } from '../useSession';
import { useHybridHistory } from '../useHybridHistory';
import { useHybridTerminal } from '../useHybridTerminal';
import { usePuppetTail } from '../usePuppetCard';
import { encodeInput, humanInput, STOP } from '../hybridProtocol';
import { HybridChat } from './HybridChat';
import { HybridTerminal } from './HybridTerminal';
import { PuppetTailSnapshot } from './PuppetTailSnapshot';
import './hybridWorkspace.css';

export function HybridWorkspace({ session, target, role, actions, close }: {
  session: Session;
  target: string | null;
  role?: string | null;
  actions?: ReactNode;
  close?: ReactNode;
}) {
  const workspaceId = useId();
  const [tab, setTab] = useState<string | null>('chat');
  const [showMetadata, setShowMetadata] = useState(false);
  return <Tabs id={workspaceId} value={tab} onChange={setTab} keepMounted keepMountedMode="display-none" className="hybrid-workspace">
    <div role="toolbar" aria-label="Рабочая область" className="hybrid-navigation">
      <Tabs.List aria-label="Рабочая область сессии">
        <Tabs.Tab value="chat">Чат</Tabs.Tab><Tabs.Tab value="terminal" aria-controls={`${workspaceId}-panel-terminal`}>Терминал</Tabs.Tab>
      </Tabs.List>
      <Text component="h2" fw={600} m={0} className="hybrid-target">{target}{role ? ` (${role})` : ''}</Text>
      <div className="hybrid-actions">{actions}</div>
      <div className="hybrid-metadata">
        <Checkbox size="xs" label="Отображать метаданные" checked={showMetadata} onChange={event => setShowMetadata(event.currentTarget.checked)} />
      </div>
      {close}
    </div>
    {/* Настройки и движки операций живут с панелью, приватные данные — только с целью. */}
    {target && <HybridSession key={JSON.stringify([session.login, session.role, target])}
      session={session} target={target} workspaceId={workspaceId} showTerminal={tab === 'terminal'} showMetadata={showMetadata} />}
  </Tabs>;
}

function HybridSession({ session, target, workspaceId, showTerminal, showMetadata }: {
  session: Session;
  target: string;
  workspaceId: string;
  showTerminal: boolean;
  showMetadata: boolean;
}) {
  const [attached, setAttached] = useState(false);
  const [draft, setDraft] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const [authLost, setAuthLost] = useState(false);
  const termRef = useRef<Terminal | null>(null);
  const sending = useRef(false);
  const bind = useCallback((terminal: Terminal | null) => { termRef.current = terminal; }, []);
  const sink = useMemo(() => ({ write: (data: Uint8Array) => termRef.current?.write(data), reset: () => termRef.current?.reset() }), []);
  const terminal = useHybridTerminal(session, target, sink);
  const history = useHybridHistory(session, target, true);
  const tail = usePuppetTail(session, target);
  const tailRef = useRef(tail); tailRef.current = tail;
  const pendingTail = useRef<Promise<void> | null>(null);
  const refreshTail = useCallback(() => {
    if (pendingTail.current) return pendingTail.current;
    const ride = tailRef.current.load();
    pendingTail.current = ride;
    void ride.finally(() => { if (pendingTail.current === ride) pendingTail.current = null; });
    return ride;
  }, []);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      if (!active || tailRef.current.blocked) return;
      await refreshTail();
      if (active && !tailRef.current.blocked) timer = setTimeout(tick, 3000);
    };
    void tick();
    return () => { active = false; clearTimeout(timer); pendingTail.current = null; };
  }, [refreshTail, session.login, target]);
  const openRef = useRef(terminal.open); openRef.current = terminal.open;
  useEffect(() => {
    if (attached) void openRef.current('rw', termRef.current?.cols ?? 80, termRef.current?.rows ?? 24);
  }, [attached]);
  const historySession = useRef<string | null>(null);
  useEffect(() => {
    const changed = history.session_id !== null && historySession.current !== null && history.session_id !== historySession.current;
    terminal.invalidate(changed);
    if (history.session_id !== null) historySession.current = history.session_id;
  }, [history.session_id, terminal.invalidate]);
  useEffect(() => {
    if (history.denied || tail.blocked) { setAuthLost(true); terminal.revoke(); }
  }, [history.denied, tail.blocked, terminal.revoke]);
  useEffect(() => {
    // Потерянное подключение не перехватывает writer повторно без открытия диалога.
    if (tail.loaded && tail.mode === 'screen' && !tail.error && !tail.blocked && !attached && !authLost && !history.denied) setAttached(true);
  }, [tail.loaded, tail.mode, tail.error, tail.blocked, attached, authLost, history.denied]);
  const showSnapshot = !authLost && !history.denied && (
    terminal.phase === 'disconnected' || terminal.phase === 'unknown'
    || (!attached && (tail.mode === 'initialization' || !tail.loaded || !!tail.error))
  );
  const submit = async () => {
    if (!draft.trim() || !terminal.canInput || terminal.busy || sending.current) return;
    const sent = draft;
    let parts: string[];
    try { parts = humanInput(sent); }
    catch { setNotice('Управляющие символы недопустимы в чате. Черновик сохранён.'); return; }
    sending.current = true;
    setNotice(null);
    try {
      const accepted = await terminal.send(parts);
      if (accepted) setDraft(current => current === sent ? '' : current);
      else setNotice('Отправка не подтверждена. Черновик сохранён; перед повтором проверьте терминал.');
    } finally { sending.current = false; }
  };
  return <>
    {terminal.error && <Text role="alert" c="red" size="sm" px="md" py="xs">{terminal.error}</Text>}
    <div className="hybrid-panes" style={{ display: 'flex' }}>
      <Tabs.Panel value="chat" className="hybrid-chat-pane">
        <HybridChat records={history.records} partial={history.partial} loading={history.loading} error={history.error} showMetadata={showMetadata} />
        <Box p="md" className="hybrid-composer">
          <div className="hybrid-compose-row">
            <Textarea className="hybrid-input" aria-label="Ввод человека в CLI" placeholder="Сообщение или /команда" autosize minRows={2} maxRows={6} value={draft}
              onChange={event => setDraft(event.currentTarget.value)}
              onKeyDown={event => {
                if (event.key === 'Escape') {
                  event.preventDefault();
                  event.stopPropagation();
                  if (terminal.canInput && !event.repeat && !event.nativeEvent.isComposing && event.keyCode !== 229) void terminal.send(encodeInput(STOP));
                  return;
                }
                if (event.key !== 'Enter' || event.nativeEvent.isComposing || event.keyCode === 229) return;
                event.preventDefault();
                if (event.ctrlKey) {
                  const { selectionStart, selectionEnd } = event.currentTarget;
                  event.currentTarget.setRangeText('\n', selectionStart, selectionEnd, 'end');
                  setDraft(event.currentTarget.value);
                } else void submit();
              }} />
            <Button size="sm" disabled={!terminal.canInput || terminal.busy || !draft.trim()} onClick={() => void submit()}>Отправить</Button>
          </div>
          {notice && !terminal.error && <Text role="alert" c="red" size="sm" mt={4}>{notice}</Text>}
        </Box>
      </Tabs.Panel>
      <div role="tabpanel" id={`${workspaceId}-panel-terminal`} aria-labelledby={`${workspaceId}-tab-terminal`}
        className="hybrid-terminal-pane" style={{ display: showTerminal ? 'flex' : 'none' }}>
        <div className="hybrid-live-terminal" style={{ display: showSnapshot ? 'none' : 'flex' }}>
          {attached && <HybridTerminal visible={showTerminal && !showSnapshot} writable={terminal.canInput} bind={bind} send={terminal.send} resize={terminal.resize} />}
        </div>
        <PuppetTailSnapshot tail={tail} visible={showTerminal && showSnapshot} refresh={refreshTail} />
      </div>
    </div>
  </>;
}
