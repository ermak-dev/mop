// Разрушительные действия (#473): recycle/delete/wipe -- три отдельных
// действия, никаких обобщённых «force». Каждое -- двухшаговое:
// (1) preview последствий, свой для каждого действия (клон/тело/джоб),
// (2) подтверждение точной цели -- имя, узел и state-строка с экрана.
// Stale-защита: поток держит строку, на которую открыт; строка сменилась
// (rerender принёс другую цель) -- подтверждение умирает, повтор начинается
// заново, с preview: молча ретраить по имени нельзя, под именем мог
// оказаться другой экземпляр. Частичный исход -- «частично», recovery
// не обещается. Никакого force: params пусты, схема -- #461.
// #516: точка входа -- пункт окна правки (icon=false + сигнал-токен с
// именем действия); инлайн-вердикта при живом ходе больше нет -- вердикт
// показывает OperationResult, единый механизм с рестартом/правкой
// (#465/#473): кнопки уехали из строки, строке вердикт показывать некому.
import { useRef, useState } from "react";
import { Button, Code, Group, Modal, Stack, Text } from "@mantine/core";
import type { Puppet } from "../types";
import type { Session } from "../useSession";
import { TERMINAL, useAdminOp } from "../useAdminOp";
import { useSignal, type Signal as EntrySignal } from "./ControlWindow";
import { OperationResult } from "./OperationResult";
import { opWord } from "./RestartBox";

/** Последствия действия для preview (#456/#471): что гибнет, что живёт. */
const PREVIEW: Record<string, { title: string; lines: string[] }> = {
  recycle: { title: "последствия recycle", lines: [
    "клон будет возвращён к origin/ветке: несохранённая работа погибнет",
    "каталог цели будет вычищен целиком: первая сборка после recycle медленная",
    "джоб и тело останутся жить: папет поднимется заново на чистом клоне",
  ]},
  delete: { title: "последствия delete", lines: [
    "клон и джоб удаляются, папета больше нет",
    "тело -- контейнер: уйдут память и диск вместе с ним",
  ]},
  wipe: { title: "последствия wipe", lines: [
    "клон будет сброшен к HEAD, каталог цели вычищен",
    "тело пересоздаётся, разговор теряется",
    "живая сессия -- отказ: wipe для остановленного, полный цикл -- recycle",
  ]},
};

const ACTIONS = ["recycle", "delete", "wipe"] as const;

/** Сигнал пункта окна с именем действия (#516): окно знает, какой пункт
 * нажат; срабатывание -- по изменению токена. */
export type DestructiveSignal = { sub: string; at: number } | null;

export function DestructiveBox({ session, row, icon = true, signal = null }: {
  session: Session;
  /** Строка ростера, на которую открыт поток: её факты и подтверждает. */
  row: Puppet;
  /** Кожа кнопок в строке (#516): пункт окна шлёт сигнал без кнопок. */
  icon?: boolean;
  /** Сигнал-токен пункта окна (#516): sub -- имя действия. */
  signal?: DestructiveSignal;
}) {
  const [action, setAction] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [result, setResult] = useState(false);
  // Строка, на которую открыт поток. Смена цели между preview и
  // подтверждением -- подтверждение не той цели (#473): умерщвляем поток
  // в рендере (паттерн useJournal -- prev сверяется до краски), повтор
  // начинается заново, с preview.
  const flowFor = useRef<string | null>(null);
  if ((action || confirm) && flowFor.current !== row.name) {
    flowFor.current = null;
    if (action) setAction(null);
    if (confirm) setConfirm(null);
  }
  const { op, submit } = useAdminOp(session, session.role === "admin" ? row.name : null);
  const live = op.status !== "idle" && !TERMINAL.has(op.status);
  // Вход пунктом окна (#516): сигнал с именем действия открывает preview;
  // живой ход показывает результат (повтор -- после вердикта).
  const enter = useRef((sub: string) => {
    if (live) { setResult(true); return; }
    flowFor.current = row.name;
    setAction(sub); setConfirm(null);
  });
  useSignal(signal as EntrySignal, () => enter.current(signal!.sub));
  if (session.role !== "admin") return null;
  return (
    <Group gap="sm">
      {icon && ACTIONS.map((a) => (
        <Button key={a} size="xs" variant="light" color="red"
                disabled={live}
                onClick={() => { flowFor.current = row.name; setAction(a); setConfirm(null); }}>
          {a}
        </Button>
      ))}
      <OperationResult opened={result} onClose={() => setResult(false)}
        onAgain={() => { setResult(false); setAction(null); setConfirm(null); }}
        title={confirm ?? action ?? "операция"} target={row.name}
        op={op} word={opWord(op.status)} testId="destructive-verdict" />
      {/* Шаг 1: preview последствий выбранного действия. */}
      {action && !confirm && (
        <Modal opened onClose={() => setAction(null)} size="md"
               title={PREVIEW[action].title} data-testid="destructive-preview"
               closeButtonProps={{ "aria-label": "закрыть" }}>
          <Stack gap="sm">
            {PREVIEW[action].lines.map((l) => (
              <Text key={l} size="sm">{l}</Text>
            ))}
            <Text size="sm" c="dimmed">сила (force) не применяется</Text>
            <Group justify="flex-end" gap="sm">
              <Button variant="light" color="gray"
                      onClick={() => setAction(null)}>отмена</Button>
              {/* второй шаг -- подтверждение точной цели: новый модал,
                  новый просмотр фактов строки */}
              <Button color="red"
                      onClick={() => setConfirm(action)}>продолжить</Button>
            </Group>
          </Stack>
        </Modal>
      )}
      {/* Шаг 2: подтверждение -- ровно то, что на экране. Умирает вместе
          со строкой: rerender с другой целью его не оставляет. */}
      {confirm && (
        <Modal opened onClose={() => { setConfirm(null); setAction(null); }}
               size="md" title={`подтвердите ${confirm}`}
               data-testid="destructive-confirm"
               closeButtonProps={{ "aria-label": "закрыть" }}>
          <Stack gap="sm">
            <Text size="sm">действие: <Text span fw={600}>{confirm}</Text></Text>
            <Text size="sm">цель: <Text span fw={600}>{row.name}</Text></Text>
            <Text size="sm">узел: {row.node}</Text>
            <Text size="sm">состояние: <Code>{row.state}</Code></Text>
            <Text size="sm" c="dimmed">
              частичный исход показывается как есть; восстановления не обещается
            </Text>
            <Group justify="flex-end" gap="sm">
              <Button variant="light" color="gray"
                      onClick={() => { setConfirm(null); setAction(null); }}>
                отмена
              </Button>
              <Button color="red" onClick={() => {
                const a = confirm;
                setConfirm(null); setAction(null);
                setResult(true);
                void submit(a);
              }}>
                подтвердить
              </Button>
            </Group>
          </Stack>
        </Modal>
      )}
    </Group>
  );
}
