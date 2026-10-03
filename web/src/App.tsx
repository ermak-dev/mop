// Приложение дашборда (#296): шапка со счётчиками корзин, ошибки сборщика и
// секции -- пул и узлы (#298), расход (#299). Журнал (#422) -- отдельная
// страница на #journal для админа: событиям больше нет места на главной,
// их источник -- закрытый /api/journal (#424), а не публичный снимок.
import { Alert, Container, Group, Stack, Tabs, Text } from "@mantine/core";
import { useEffect, useRef, useState } from "react";
import { usePool } from "./usePool";
import { fetchSession, GUEST, LoginRefused, postLogin, postLogout, type Session } from "./useSession";
import { useJournal } from "./useJournal";
import { useHashPage } from "./useHashPage";
import { SessionBox } from "./components/SessionBox";
import { PoolTable } from "./components/PoolTable";
import { NodesPanel } from "./components/NodesPanel";
import { Masters } from "./components/Masters";
import { JournalPage } from "./components/JournalPage";
import { UsageChart } from "./components/UsageChart";
import { UsageByUser } from "./components/UsageByUser";
import { LoadBadges, LoadBar } from "./components/PoolLoad";
import { ThemeSwitch } from "./components/ThemeSwitch";

// Слова, цвета корзин и склонение -- одна таблица на страницу (#301, #323):
// components/format.ts. Бейджи шапки и полоса аллокаций -- места пула от
// сервера (#378): components/PoolLoad.tsx.

export default function App() {
  const { snapshot } = usePool();
  const { page, go } = useHashPage();
  // Сессия (#416): до ответа -- гость, 401/сбой панели не рвут публичные
  // таблицы; истёкшая cookie -- тот же гость.
  const [session, setSession] = useState<Session>(GUEST);
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
  const logout = async () => {
    try {
      setSession(await postLogout());
      setFailed(null);
      authEpoch.current++;
    } catch {
      setPanelError("панель недоступна");
    }
  };
  const onJournal = page === "journal";
  return (
    <Container size="xl" pt="md" pb="md">
      <Stack gap="md">
        <Group justify="space-between" align="center">
          <Tabs value={page} onChange={(value) => {
            if (value === "main" || value === "journal") go(value);
          }}>
            <Tabs.List aria-label="страницы">
              <Tabs.Tab value="main">Пул</Tabs.Tab>
              {session.role === "admin" && <Tabs.Tab value="journal">Журнал</Tabs.Tab>}
            </Tabs.List>
          </Tabs>
          <Group gap="md">
            {panelError && <Text c="red" size="sm">{panelError}</Text>}
            <SessionBox session={session} onLogin={login} onLogout={logout} failed={failed} />
            <ThemeSwitch />
          </Group>
        </Group>
        <Group gap="sm">
          <img src="/logo.png" alt="" width={28} height={28} />
          {snapshot ? <LoadBadges load={snapshot.load} counts={snapshot.counts} />
            : <Text c="dimmed">снимок ещё не пришёл</Text>}
        </Group>
        {!onJournal && snapshot?.load && <LoadBar load={snapshot.load} />}
        {onJournal ? (
          <JournalPage journal={journal.journal} error={journal.error} open={() => {}}
                       sourceKey={`${session.login ?? ""}\0${session.role ?? ""}`} />
        ) : (
        <Stack gap="md">
          {snapshot && snapshot.errors.length > 0 && (
            <Alert color="red" variant="light" title="сборщик">{snapshot.errors.join("\n")}</Alert>
          )}
          {snapshot && (
            <>
              <PoolTable projects={snapshot.projects} />
              {/* живые мастера (#305) */}
              <Masters masters={snapshot.masters} every={snapshot.masters_every} />
              <NodesPanel nodes={snapshot.nodes} />
              {/* расход (#299) */}
              <UsageChart days={snapshot.usage} />
              <UsageByUser users={snapshot.per_user} />
              {/* журнал -- отдельная страница админа (#422), на главной его нет */}
            </>
          )}
        </Stack>
        )}
      </Stack>
    </Container>
  );
}
