// Окно управления целью (#516 -> #520): клик «правка» в строке
// открывает визард -- Mantine Stepper: выбор действия -> параметры ->
// подтверждение. Первый экран -- все шесть действий (обновление,
// модель, рестарт, recycle/delete/wipe), вложения «правка -> правка»
// нет: пункт update зовётся «обновление». Рестарт -- без параметров и
// без подтверждения: клик шлёт POST сразу (#513), Stepper не
// переключается. Обратимые (update keep, модель) пропускают шаг
// подтверждения: «выполнить» шлёт приказ сразу. Необратимые (update
// replace/remove, разрушительные) -- единый финальный экран:
// последствия + факты точной цели + одна кнопка «подтвердить» (прежде
// у разрушительных было два экрана: preview #473 и подтверждение
// #465). Движки не меняются: боксы (useAdminOp + OperationResult)
// остаются смонтированными в строке, визард шлёт им приказ-токен
// {at, act, params} -- наследник сигнал-механики #516; их состояние
// переживает окно. Гард #473: смена цели под открытым визардом (шаг
// параметров или дальше) сбрасывает к выбору действия -- подтверждение
// умирает, повтор начинается с начала, молча ретраить по имени нельзя.
import { useEffect, useRef, useState } from "react";
import { Button, Code, Group, Modal, Radio, Stack, Stepper, Text, TextInput, Tooltip } from "@mantine/core";
import type { Puppet } from "../types";
import type { Session } from "../useSession";
import { useModels } from "../useModels";
import { restartReason } from "./RestartBox";
import { MODES } from "./UpdateBox";
import { PREVIEW } from "./DestructiveBox";
import { LABELS } from "./labels";

/** Какое действие запускает визард (#520). */
export type ControlEntry = "update" | "model" | "restart" | "recycle" | "delete" | "wipe";

/** Приказ движку (#520): токен меняется на каждый приказ, движок
 * срабатывает по изменению; mount молчит. */
export type Signal = { at: number; act: string; params: Record<string, unknown> } | null;

/** Приказ сработал (#520): срабатывание -- по изменению токена; первый
 * рендер (mount) молчит -- иначе монтаж движка стартовал бы поток. fire
 * держится через latest-ref: замыкание видит свежий стейт движка, а не
 * первый рендер. */
export function useSignal(signal: Signal, fire: () => void) {
  const seen = useRef(signal);
  const latest = useRef(fire);
  latest.current = fire;
  useEffect(() => {
    if (signal && signal !== seen.current) {
      seen.current = signal;
      latest.current();
    }
  }, [signal]);
}

/** Приказ конкретному действию (#520): движков на строку четыре, приказ
 * один -- каждый берёт свой, чужой молча пропускает (seen всё равно
 * двигается: старший приказ не должен выстрелить позже). */
export function useOrder(order: Signal, act: string,
                         fire: (params: Record<string, unknown>) => void) {
  const seen = useRef(order);
  const latest = useRef(fire);
  latest.current = fire;
  useEffect(() => {
    if (order && order !== seen.current) {
      seen.current = order;
      if (order.act === act) latest.current(order.params);
    }
  }, [order, act]);
}

/** Разрушительные -- три отдельных действия сервера (#473). */
const RUIN = ["recycle", "delete", "wipe"] as const;
const isRuin = (e: string | null): e is (typeof RUIN)[number] =>
  !!e && (RUIN as readonly string[]).includes(e);

/** Слово GUI для действия на финальном экране -- словарь #521. */
const ENTRY_WORD: Record<string, string> = {
  update: LABELS.update, model: LABELS.model, restart: LABELS.restart,
  recycle: LABELS.recycle, delete: LABELS.delete, wipe: LABELS.wipe,
};

export function ControlWindow({ opened, onClose, onOrder, row, session,
                               initialEntry = null, engineState = {}, onReveal }: {
  opened: boolean;
  onClose: () => void;
  /** Приказ движку: подтверждённый ход с параметрами визарда. */
  onOrder: (order: { act: string; params: Record<string, unknown> }) => void;
  row: Puppet;
  session: Session;
  /** Открыть сразу на шаге действия (#520): «новая команда» в вердикте
   * обновления возвращает к повторному выбору режима. */
  initialEntry?: ControlEntry | null;
  /** Состояния движков по act (#487): непустой статус -- непоказанный
   * ход; повторный вход показывает его вердикт, не форму. */
  engineState?: Record<string, string>;
  /** Приказ движку показать сохранённый вердикт (#487). */
  onReveal?: (act: string) => void;
}) {
  // Шаг визарда: 0 -- выбор действия, 1 -- параметры, 2 -- подтверждение.
  // Открытие сразу на шаге действия (#520, «новая команда»): визард
  // смонтирован всегда и возвращает null закрытым, инициализатор стейта
  // срабатывает один раз -- смену initialEntry ловит prev-ref (паттерн
  // useJournal): свежий вход -- до краски, первого рендера со старым
  // шагом нет.
  const [step, setStep] = useState(initialEntry ? (isRuin(initialEntry) ? 2 : 1) : 0);
  const [entry, setEntry] = useState<ControlEntry | null>(initialEntry);
  // Гард #473: строка, на которую открыт визард. Смена цели под шагом
  // параметров или дальше сбрасывает к выбору -- подтверждение не той
  // цели умирает, повтор начинается заново (паттерн useJournal: prev
  // сверяется до краски, первый рендер уже без прежнего шага).
  const flowFor = useRef<string | null>(initialEntry ? row.name : null);
  const prevEntry = useRef<ControlEntry | null | undefined>(initialEntry);
  if (prevEntry.current !== initialEntry) {
    prevEntry.current = initialEntry;
    if (initialEntry) {
      setStep(isRuin(initialEntry) ? 2 : 1);
      setEntry(initialEntry);
      flowFor.current = row.name;
    } else { setStep(0); setEntry(null); }
  }
  const [mode, setMode] = useState<keyof typeof MODES | null>(null);
  const [origin, setOrigin] = useState("");
  const [model, setModel] = useState<string | null>(null);
  // Каталог моделей: спрашивается при открытом шаге модели (#468)
  const { models, error: catalogError } = useModels(
    session, session.role === "admin" && opened && entry === "model");
  if (opened && step > 0 && flowFor.current !== row.name) {
    flowFor.current = null;
    setStep(0); setEntry(null); setMode(null); setOrigin(""); setModel(null);
  }
  if (!opened) return null;
  const blocked = session.role === "admin" ? restartReason(row) : null;
  // Готовность шага параметров: без выбора (и origin для пересадки)
  // дальше пути нет
  const ready = entry === "update"
    ? mode !== null && (mode !== "remove" || origin.trim() !== "")
    : entry === "model" ? model !== null
    : false;
  // Необратимость выбранного хода: replace/remove и разрушительные --
  // единый финальный экран; keep и модель пропускают подтверждение
  const ruin = isRuin(entry) || (entry === "update" && mode !== null && mode !== "keep");
  const params: Record<string, unknown> =
    entry === "update" && mode === "keep" ? { fresh: false }
    : entry === "update" && mode === "replace" ? { fresh: true }
    : entry === "update" && mode === "remove" && origin.trim() ? { fresh: true, origin: origin.trim() }
    : entry === "model" && model ? { model }
    : {};
  // Приказ: «выполнить» (обратимые) и «подтвердить» (необратимые) --
  // один и тот же ход; визард закрывается исполнением, состояние
  // умирает вместе с ним (PuppetActions монтирует окно на время)
  const order = (act: string, p: Record<string, unknown>) => {
    onOrder({ act, params: p });
    flowFor.current = null;
    setStep(0); setEntry(null); setMode(null); setOrigin(""); setModel(null);
    onClose();
  };
  // Пункт первого экрана: рестарт шлёт сразу (#513), разрушительные
  // идут сразу на подтверждение (параметров у них нет), остальные --
  // шаг параметров. Повторный вход при непоказанном ходе (#487/#513):
  // движок рапортует непустой статус -- визард приказывает показать
  // сохранённый вердикт и закрывается; второго POST нет. Модель (#488)
  // исключение: её вердикт -- балун, повторный вход открывает форму.
  const ENGINE_OF: Record<string, string> = {
    update: "update", restart: "restart",
    recycle: "destructive", delete: "destructive", wipe: "destructive",
  };
  const pick = (e: ControlEntry) => {
    const held = ENGINE_OF[e] && engineState[ENGINE_OF[e]] && engineState[ENGINE_OF[e]] !== "idle";
    if (e === "restart" && !held) { order("restart", {}); return; }
    if (held) { onReveal?.(ENGINE_OF[e]); onClose(); return; }
    flowFor.current = row.name;
    setEntry(e);
    setStep(isRuin(e) ? 2 : 1);
  };
  // Последствия выбранного хода и факты цели -- на финальный экран
  const lines = isRuin(entry) ? PREVIEW[entry].lines
    : entry === "update" && mode ? MODES[mode].lines : [];
  return (
    <Modal opened onClose={onClose} size="md" title="правка папета"
           data-testid="control-window"
           closeButtonProps={{ "aria-label": LABELS.close }}>
      <Stepper active={step} onStepClick={setStep} allowNextStepsSelect={false}>
        <Stepper.Step label="действие" description={row.name}>
          <Stack gap="xs">
            <Text size="sm">цель: <Text span fw={600}>{row.name}</Text></Text>
            <Button variant="light" onClick={() => pick("update")}>{LABELS.update}</Button>
            <Button variant="light" onClick={() => pick("model")}>{LABELS.model}</Button>
            <Tooltip label={blocked ?? ""} disabled={!blocked}>
              <Button variant="light" color="orange" disabled={!!blocked}
                      aria-label={blocked ?? LABELS.restart}
                      onClick={() => pick("restart")}>{LABELS.restart}</Button>
            </Tooltip>
            {/* разрушительные -- своим красным, последствия -- на
                едином экране подтверждения (#512/#473) */}
            {RUIN.map((a) => (
              <Button key={a} variant="light" color="red" onClick={() => pick(a)}>
                {a === "recycle" ? LABELS.recycle : a === "delete" ? LABELS.delete : LABELS.wipe}
              </Button>
            ))}
          </Stack>
        </Stepper.Step>
        <Stepper.Step label="параметры" description={entry ? ENTRY_WORD[entry] : ""}>
          <Stack gap="sm" data-testid={entry === "model" ? "model-choices" : "update-modes"}>
            <Text size="sm">цель: <Text span fw={600}>{row.name}</Text></Text>
            {entry === "update" && (
              <>
                <Radio.Group value={mode} onChange={(v) => setMode(v as keyof typeof MODES)}>
                  <Stack gap={8} mt="xs">
                    {(Object.keys(MODES) as (keyof typeof MODES)[]).map((k) => (
                      <Radio key={k} value={k} label={MODES[k].label} />
                    ))}
                  </Stack>
                </Radio.Group>
                {/* последствия выбранного -- до подтверждения, слова #473 */}
                {mode && MODES[mode].lines.map((l) => (
                  <Text key={l} size="sm" c="dimmed">{l}</Text>
                ))}
                {mode === "remove" && (
                  <TextInput label="новый origin" value={origin}
                             onChange={(e) => setOrigin(e.target.value)}
                             data-testid="update-origin" />
                )}
              </>
            )}
            {entry === "model" && (
              <>
                {catalogError ? <Text size="sm" c="red">{catalogError}</Text> : null}
                {models.length ? (
                  <Radio.Group value={model} onChange={(v) => setModel(v)}>
                    <Stack gap={8} mt="xs">
                      {models.map((m) => {
                        // текущая модель мечена и заперта (#488)
                        const current = row.llm && row.llm !== "-" ? row.llm : null;
                        return (
                          <Radio key={m} value={m}
                                 label={m === current ? `${m} — текущая` : m}
                                 disabled={m === current} />
                        );
                      })}
                    </Stack>
                  </Radio.Group>
                ) : !catalogError ? (
                  <Text c="dimmed" size="sm">каталог грузится</Text>
                ) : null}
              </>
            )}
            <Group justify="flex-end" gap="sm">
              <Button variant="light" color="gray" onClick={() => setStep(0)}>{LABELS.back}</Button>
              {ruin
                ? <Button disabled={!ready} onClick={() => setStep(2)}>{LABELS.next}</Button>
                : <Button disabled={!ready} onClick={() => { if (entry) order(entry, params); }}>
                    {LABELS.run}
                  </Button>}
            </Group>
          </Stack>
        </Stepper.Step>
        <Stepper.Step label="подтверждение" description="необратимо">
          <Stack gap="sm">
            {/* факты точной цели -- ровно то, что на экране (#465) */}
            <Text size="sm">действие: <Text span fw={600}>{entry ? ENTRY_WORD[entry] : ""}</Text></Text>
            <Text size="sm">цель: <Text span fw={600}>{row.name}</Text></Text>
            <Text size="sm">узел: {row.node}</Text>
            <Text size="sm">состояние: <Code>{row.state}</Code></Text>
            {lines.map((l) => <Text key={l} size="sm" c="dimmed">{l}</Text>)}
            {entry === "update" && mode === "remove" && (
              <Text size="sm">новый origin: <Code>{origin.trim()}</Code></Text>
            )}
            {isRuin(entry) && (
              <Text size="sm" c="dimmed">сила (force) не применяется</Text>
            )}
            <Text size="sm" c="dimmed">
              частичный исход показывается как есть; восстановления не обещается
            </Text>
            <Group justify="flex-end" gap="sm">
              <Button variant="light" color="gray"
                      onClick={() => setStep(isRuin(entry) ? 0 : 1)}>{LABELS.back}</Button>
              <Button color={isRuin(entry) ? "red" : "blue"}
                      onClick={() => {
                        if (isRuin(entry)) order(entry, {});
                        else if (entry === "update") order("update", params);
                      }}>
                {LABELS.confirm}
              </Button>
            </Group>
          </Stack>
        </Stepper.Step>
      </Stepper>
    </Modal>
  );
}
