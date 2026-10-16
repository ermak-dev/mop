// Журнал админа читает закрытый /api/journal (#424), не публичный снимок.
// Панель сессии и движки операций не принадлежат фильтру ростера.
import { Alert, Group, Stack, Tabs, Text } from "@mantine/core";
import { useEffect, useRef, useState } from "react";
import { usePool } from "./usePool";
import { useOperationBalloons } from "./useOperationBalloons";
import { fetchSession, GUEST, hasControls, LoginRefused, postLogin, postLogout, type Session } from "./useSession";
import { useJournal } from "./useJournal";
import { useHashPage } from "./useHashPage";
import { SessionBox } from "./components/SessionBox";
import { PoolTable } from "./components/PoolTable";
import { DashboardSplit } from "./components/DashboardSplit";
import { PuppetCard } from "./components/PuppetCard";
import { NodesPanel } from "./components/NodesPanel";
import { roster } from "./roster";
import { JournalPage } from "./components/JournalPage";
import { UsageChart } from "./components/UsageChart";
import { UsageByUser } from "./components/UsageByUser";
import { LoadBadges, LoadBar } from "./components/PoolLoad";
import { ThemeSwitch } from "./components/ThemeSwitch";
import './dashboard.css';

// Слова, цвета корзин и склонение -- одна таблица на страницу (#301, #323):
// components/format.ts. Бейджи шапки и полоса аллокаций -- места пула от
// сервера (#378): components/PoolLoad.tsx.

export default function App() {
  const [session, setSession] = useState<Session>(GUEST);
  const { snapshot, stale, error: poolError } = usePool(session);
  // Балун — уведомление после применения Operation в общем store, не
  // второй канал статуса. Неизвестный результат восстанавливается через GET.
  useOperationBalloons();
  const { page, go } = useHashPage();
  // До подтверждённой Django identity приватный ростер не читается.
  const [failed, setFailed] = useState<string | null>(null);
  const [panelError, setPanelError] = useState<string | null>(null);
  // Журнал (#422): закрытый источник, тянет только admin и только пока
  // страница журнала открыта (опрос каждые POLL_S, ревью #422); на 401/403
  // и смену роли записи стирает сам хук -- на экране не остаётся прежних.
  const journal = useJournal(session, page === "journal");
  // Стартовый опрос и обновление на возврат к вкладке (#416, ревью): cookie
  // сессии живёт TTL, и открытая вкладка без reload показывала бы админ-ссылку
  // и логин вечно. Порядок без гонки со входом: опрос кладёт ответ только
  // если после него не было локального входа/выхода -- счётчик версий.
  const authEpoch = useRef(0);
  // Поколение запросов: отвечает только последний НАЧАТЫЙ sync. Два опроса
  // (focus + visibility) с одной эпохой -- старший ответ, пришедший позже
  // младшего, не возвращает прошлое состояние. authEpoch отдельно: локальный
  // вход/выход отменяет и висящие опросы.
  const syncGen = useRef(0);
  const sync = () => {
    const at = authEpoch.current;
    const gen = ++syncGen.current;
    fetchSession()
      .then((s) => { if (at === authEpoch.current && gen === syncGen.current) setSession(s); })
      .catch(() => {
        if (at === authEpoch.current && gen === syncGen.current) setPanelError("панель недоступна");
      });
  };
  useEffect(() => {
    sync();
    const onFocus = () => sync();
    document.addEventListener("visibilitychange", onFocus);
    window.addEventListener("focus", onFocus);
    return () => {
      document.removeEventListener("visibilitychange", onFocus);
      window.removeEventListener("focus", onFocus);
    };
  }, []);

  // Возврат -- успех входа: SessionBox закрывает окно только на true.
  // Отказ (401/429/сбой) не бросаем дальше: это не ошибка страницы.
  const login = async (login: string, password: string): Promise<boolean> => {
    try {
      setSession(await postLogin(login, password));
      setFailed(null);
      // успех входа снимает и прошлую ошибку панели: страница снова в порядке
      setPanelError(null);
      authEpoch.current++;
      return true;
    } catch (e) {
      // 401 -- сервер отказал, 429 -- лимит попыток; остальное -- панель
      setFailed(e instanceof LoginRefused
        ? (e.status === 401 ? "неверный логин или пароль" : e.reason)
        : "панель недоступна");
      return false;
    }
  };
  // Карточка папета (#463): цель держит App, не таблица -- закрытие и смена
  // роли/логина гасят её отсюда одним setState, а опоздавший ответ прежней
  // цели отбрасывает сам хук (поколение). Гостю и не-admin опенера нет.
  const identity = JSON.stringify([session.login, session.role, session.generation]);
  const [selection, setSelection] = useState<{ name: string; identity: string } | null>(null);
  const puppet = selection?.identity === identity ? selection.name : null;
  const setPuppet = (name: string | null) => setSelection(name ? { name, identity } : null);
  const closePuppet = () => setSelection(null);
  useEffect(() => { setSelection(null); }, [page, identity]);
  const logout = async () => {
    try {
      setSession(await postLogout());
      setFailed(null);
      authEpoch.current++;
      // открытая карточка не переживает выход (#463): факты админа не
      // остаются на экране гостя
      setPuppet(null);
    } catch {
      setPanelError("панель недоступна");
    }
  };
  const managed = snapshot ? roster(snapshot.projects, snapshot.sessions, snapshot.masters)
    .filter(entry => entry.source === 'managed') : [];
  const selected = managed.find(entry => entry.name === puppet);
  const target = page === 'main' && hasControls(session) && selected ? selected.name : null;
  const screenRows = managed.map(entry => ({ ...entry, owner: entry.user || '', origin: entry.origin || '?' }));
  return (
    <div className="dashboard" data-testid="dashboard">
      <Group justify="space-between" align="center" className="dashboard-header">
        <Tabs value={page} onChange={(value) => {
          if (value === 'main' || value === 'nodes' || value === 'tokens' || value === 'journal') go(value);
        }}>
          <Tabs.List aria-label="страницы">
            <Tabs.Tab value="main">Пул</Tabs.Tab>
            <Tabs.Tab value="nodes">Узлы</Tabs.Tab>
            <Tabs.Tab value="tokens">Токены</Tabs.Tab>
            {hasControls(session) && <Tabs.Tab value="journal">Журнал</Tabs.Tab>}
          </Tabs.List>
        </Tabs>
        <Group gap="md">
          {panelError && <Text c="red" size="sm">{panelError}</Text>}
          <SessionBox session={session} onLogin={login} onLogout={logout} failed={failed} />
          <ThemeSwitch />
        </Group>
      </Group>
      <Group gap="sm">
        {stale && <Text size="sm" c="dimmed">Realtime is stale; REST reconciliation is active</Text>}
        {poolError && <Text role="alert" size="sm" c="red">{poolError}</Text>}
        <img src="/logo.png" alt="" width={28} height={28} />
        {snapshot ? <LoadBadges load={snapshot.load} counts={snapshot.counts} />
          : <Text c="dimmed">снимок ещё не пришёл</Text>}
      </Group>
      {page === 'main' && snapshot?.load && <LoadBar load={snapshot.load} />}
      <DashboardSplit open={!!target} main={
        <main className="dashboard-main">
          {page === 'journal' ? (
            <JournalPage journal={journal.journal} error={journal.error} open={() => {}}
              sourceKey={identity} />
          ) : <Stack gap="md">
            {snapshot && snapshot.errors.length > 0 && <Alert color="red" variant="light" title="сборщик">{snapshot.errors.join('\n')}</Alert>}
            {page === 'main' && <>
              <Text size="sm" c="dimmed">Creation is not yet exposed by the new controller.</Text>
              {snapshot && <PoolTable projects={snapshot.projects} sessions={snapshot.sessions}
                masters={snapshot.masters} session={session} selected={target}
                onOpen={hasControls(session) ? setPuppet : undefined} />}
            </>}
            {page === 'nodes' && snapshot && <NodesPanel nodes={snapshot.nodes} />}
            {page === 'tokens' && snapshot && <>
              <UsageChart days={snapshot.usage} />
              <UsageByUser users={snapshot.per_user} />
            </>}
          </Stack>}
        </main>
      } screen={
        <PuppetCard key={identity} session={session} role={selected?.role} rows={screenRows}
          target={target} onClose={closePuppet} />
      } />
    </div>
  );
}
