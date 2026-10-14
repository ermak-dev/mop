import { useCallback, useEffect, useMemo, useRef, useState, useId } from 'react';
import type { Terminal } from '@xterm/xterm';
import { Box, Button, Group, Switch, Tabs, Text, Textarea } from '@mantine/core';
import { useMediaQuery } from '@mantine/hooks';
import type { Session } from '../useSession';
import { useHybridHistory } from '../useHybridHistory';
import { useHybridTerminal } from '../useHybridTerminal';
import { usePuppetTail } from '../usePuppetCard';
import { encodeInput, humanInput, STOP } from '../hybridProtocol';
import { HybridChat } from './HybridChat';
import { HybridTerminal } from './HybridTerminal';
import { PuppetTailSnapshot } from './PuppetTailSnapshot';
import { SendForm } from './SendForm';
import './hybridWorkspace.css';

export function HybridWorkspace({ session, target }: { session: Session; target: string }) {
  const workspaceId = useId();
  const small = useMediaQuery('(max-width: 700px)');
  const [tab, setTab] = useState<string | null>('chat');
  const [split, setSplit] = useState(false);
  const [snapshotOpen, setSnapshotOpen] = useState(false);
  const [intent, setIntent] = useState<{ mode: 'ro' | 'rw'; number: number } | null>(null);
  const [draft, setDraft] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const [authLost, setAuthLost] = useState(false);
  const termRef = useRef<Terminal | null>(null);
  const bind = useCallback((terminal: Terminal | null) => { termRef.current = terminal; }, []);
  const sink = useMemo(() => ({ write: (data: Uint8Array) => termRef.current?.write(data), reset: () => termRef.current?.reset() }), []);
  const terminal = useHybridTerminal(session, target, sink);
  const history = useHybridHistory(session, target, tab === 'chat');
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
    if (intent) void openRef.current(intent.mode, termRef.current?.cols ?? 80, termRef.current?.rows ?? 24);
  }, [intent]);
  const historySession = useRef<string | null>(null);
  useEffect(() => {
    const changed = history.session_id !== null && historySession.current !== null && history.session_id !== historySession.current;
    terminal.invalidate(changed);
    if (history.session_id !== null) historySession.current = history.session_id;
  }, [history.session_id, terminal.invalidate]);
  useEffect(() => {
    if (history.denied || tail.blocked) { setAuthLost(true); terminal.revoke(); }
  }, [history.denied, tail.blocked, terminal.revoke]);
  const request = (mode: 'ro' | 'rw') => {
    if (!authLost && (mode === 'ro' || tail.mode === 'screen'))
      setIntent(prev => ({ mode, number: (prev?.number ?? 0) + 1 }));
  };
  const showTerminal = tab === 'terminal' || (tab === 'chat' && split && !small);
  useEffect(() => {
    if (showTerminal && tail.loaded && tail.mode === 'screen' && !tail.blocked
        && !snapshotOpen && !intent && !authLost) {
      setIntent({ mode: 'ro', number: 1 });
    }
  }, [showTerminal, tail.loaded, tail.mode, tail.blocked, snapshotOpen, intent, authLost]);
  const showSnapshot = snapshotOpen || (!intent && (tail.mode === 'initialization' || !tail.loaded || !!tail.error));
  const chooseTab = (value: string | null) => setTab(value);
  const submit = async (interrupt = false) => {
    if (!draft.trim() || !terminal.canInput || terminal.busy) return;
    const sent = draft;
    let parts: string[];
    try { parts = humanInput(sent); }
    catch { setNotice('Управляющие символы недопустимы в чате. Для клавиш используйте терминал. Черновик сохранён.'); return; }
    const accepted = await terminal.send(parts, interrupt);
    if (accepted) {
      setDraft(current => current === sent ? '' : current);
      setNotice('Байты записаны в CLI. Выполнение команды не подтверждено.');
    } else setNotice('Ввод не подтверждён. Черновик сохранён; перед повтором проверьте терминал.');
  };
  const controls = <Group gap="xs" wrap="wrap">
    {!terminal.canInput && <Button size="compact-sm" disabled={authLost || tail.mode !== 'screen' || terminal.phase === 'opening'} onClick={() => request('rw')}>Взять управление</Button>}
    {terminal.canInput && <Button size="compact-sm" variant="light" onClick={() => request('ro')}>Отдать управление</Button>}
    <Button size="compact-sm" variant="default" disabled={authLost || tail.mode === 'initialization' || terminal.phase === 'opening'} onClick={() => request('ro')}>Переподключить чтение</Button>
    {showTerminal && <Button size="compact-sm" variant="default" onClick={() => {
      setSnapshotOpen(current => !current);
      if (!snapshotOpen) void refreshTail();
    }}>{snapshotOpen ? 'Вернуться в терминал' : 'Последние строки'}</Button>}
    <Button size="compact-sm" variant="default" disabled={!terminal.canInput} onClick={() => void terminal.send(encodeInput(STOP))}>Stop: отправить Escape</Button>
    {intent && <Button size="compact-sm" variant="subtle" onClick={() => void terminal.close()}>Закрыть подключение</Button>}
  </Group>;
  return <Tabs id={workspaceId} value={tab} onChange={chooseTab} keepMounted keepMountedMode="display-none" className="hybrid-workspace">
    <Tabs.List px="md" aria-label="Рабочая область сессии">
      <Tabs.Tab value="chat">Чат</Tabs.Tab><Tabs.Tab value="terminal" aria-controls={`${workspaceId}-panel-terminal`}>Терминал</Tabs.Tab>
    </Tabs.List>
    <Box px="md" py="xs" className="hybrid-controls">
      <Group justify="space-between" gap="xs" mb="xs">
        <Text size="sm">{terminal.phase === 'opening' ? 'Подключение…' : terminal.canInput ? 'Управление: ваш ввод в CLI' : terminal.mode === 'ro' ? 'Только чтение: размер CLI не меняется' : 'Нет управления CLI'}</Text>
        {tab === 'chat' && <Switch className="hybrid-split-toggle" label="Терминал рядом" checked={split} onChange={event => {
          setSplit(event.currentTarget.checked);
        }} />}
      </Group>
      {controls}
      {terminal.error && <Text role="alert" c="red" size="sm" mt="xs">{terminal.error}</Text>}
    </Box>
    <div className={`hybrid-panes${tab === 'chat' && split && !small ? ' hybrid-panes-split' : ''}`} style={{ display: 'flex' }}>
      <Tabs.Panel value="chat" className="hybrid-chat-pane">
        <HybridChat records={history.records} partial={history.partial} loading={history.loading} error={history.error} refresh={history.load} />
        <Box p="md" className="hybrid-composer">
          <Textarea aria-label="Ввод человека в CLI" placeholder="Сообщение или /команда" autosize minRows={small ? 1 : 2} maxRows={small ? 3 : 6} value={draft} onChange={event => setDraft(event.currentTarget.value)} />
          <Group gap="xs" mt="xs">
            <Button size="sm" disabled={!terminal.canInput || terminal.busy || !draft.trim()} onClick={() => void submit()}>Добавить в CLI и отправить Enter</Button>
          </Group>
          <details key={small ? 'mobile' : 'desktop'} open={!small} className="hybrid-composer-extra">
            <summary>Команды и ввод CLI</summary>
            <Text size="xs" c="dimmed" my="xs">Вставка добавляется к текущему вводу CLI, затем отправляется Enter. Для меню, разрешений и вопросов откройте терминал.</Text>
            <Button size="compact-sm" variant="default" disabled={!terminal.canInput || !terminal.canInterruptSend || terminal.busy || !draft.trim()} onClick={() => void submit(true)}>Escape, затем добавить и отправить</Button>
            {!terminal.canInterruptSend && <Text size="xs" c="dimmed">Задержка Escape неизвестна: используйте отдельные Stop и отправку.</Text>}
            {terminal.canInterruptSend && <Text size="xs" c="dimmed">Пауза разделяет Escape и вставку, но не подтверждает завершение хода или инструмента.</Text>}
            <Group gap={4} mt="xs" aria-label="Команды CLI">
              {['/help', '/model', '/compact', '/status'].map(command => <Button key={command} size="compact-xs" variant="subtle" aria-label={`Добавить ${command} в черновик`} onClick={() => setDraft(current => current + command)}>{command}</Button>)}
            </Group>
            <Text size="xs" c="dimmed">Команды отправляются в CLI, а не считаются выполненными.</Text>
          </details>
          <details className="hybrid-composer-extra">
            <summary>Сообщение папету через канал</summary>
            <Text size="xs" c="dimmed" my="xs">Адресная доставка в очередь папета, не ввод в терминал. Для CLI используйте поле выше.</Text>
            {tail.loaded && !tail.blocked && tail.mode === 'screen'
              ? <SendForm session={session} target={target} />
              : <Text size="xs" c="dimmed">Доступно после запуска сессии.</Text>}
          </details>
          {notice && <Text role="status" size="xs" mt={4}>{notice}</Text>}
        </Box>
      </Tabs.Panel>
      {/* Терминал виден и рядом с чатом; Tabs.Panel принудительно скрывает
          неактивную вкладку, поэтому общий pane имеет собственную рамку. */}
      <div role="tabpanel" id={`${workspaceId}-panel-terminal`} aria-labelledby={`${workspaceId}-tab-terminal`}
        className="hybrid-terminal-pane" style={{ display: showTerminal ? 'flex' : 'none' }}>
        <div className="hybrid-live-terminal" style={{ display: showSnapshot ? 'none' : 'flex' }}>
          {intent ? <HybridTerminal visible={showTerminal && !showSnapshot} writable={terminal.canInput} bind={bind} send={terminal.send} resize={terminal.resize} />
            : <Text p="md" c="dimmed" size="sm">Откройте терминал для чтения или возьмите управление явно.</Text>}
        </div>
        <PuppetTailSnapshot tail={tail} visible={showTerminal && showSnapshot} refresh={refreshTail} />
      </div>
    </div>
  </Tabs>;
}
