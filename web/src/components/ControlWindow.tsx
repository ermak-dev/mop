// Окно управления целью (#516 -> #522 -> #527): ЕДИНСТВЕННЫЙ диалог
// операций цели -- любые вердикты и превью живут его экранами, вторых
// мод нет. Первый экран -- все шесть действий (обновление, модель,
// рестарт, Пересоздать/Удалить/Сбросить), каркас #520/#522 не тронут
// #527. #527: «Обновление» ведёт в собственный ТРЁХШАГОВЫЙ поток --
// выбор режима («Что вы хотите сделать с папетом?», радио с
// описаниями-последствиями) -> параметры (репозиторий у переключения;
// «продолжить работу» шаг пропускает) -> финал; радио ушло с первого
// экрана, смешения радио с кнопками чужих операций больше нет.
// Единое правило подтверждения: КАЖДАЯ операция заканчивается
// финальным экраном «что произойдёт» («Проверьте параметры
// операции»: последствия + факты точной цели), и этот экран сам
// является подтверждением -- кнопка «Завершить» = согласие.
// Immediate-POST рестарта и пропуск подтверждения у обратимых ушли.
// Модель -- три шага (каталог #468), остальные -- выбор -> финал.
// После «Завершить» визард не закрывается, а показывает экран хода:
// Loader+слово статуса в живом, терминальный вердикт с причиной и
// «(первый ответ потерян)»; футер -- «Новая команда» (по терминалу)
// и «Закрыть». Стейт операции поднимают движки
// (onState {status, error, lost}), divert/reveal-токены #487 ушли:
// повторный вход при непоказанном ходе -- сразу экран вердикта.
// Модель -- исключение #488: её вердикт -- балун, «Завершить»
// закрывает визард, повторный вход открывает форму.
// Первый экран однотонный: опасность говорит текстом финального
// экрана, не цветом; дизейбл рестарта с причиной -- #508. Гард #473:
// смена цели после выбора сбрасывает к выбору. «Назад» ничего не
// очищает. #527: словарь замен терминов (репозиторий, рабочая копия,
// контекст диалога с Claude, задача), «сила (force) не применяется»
// ушла, «частичный исход…» упрощена; title «Правка папета», size lg,
// футер шагов [Отмена/Назад] [Продолжить/Завершить]. Фиксированная
// высота шага (#527): контейнер шага держит структурный minHeight по
// максимуму содержания шагов потока -- смена режима не прыгает
// высотой, динамических полей на шаге выбора режима нет.
// Контракт окна: Modal centered, Stepper, Stack/Group gap=md, футер
// Назад default / Продолжить primary / Завершить primary, дизейбл
// при невалидном, без кастомных примитивов и хардкода цветов.
import { useEffect, useRef, useState } from "react";
import { Button, Code, Group, Loader, Modal, Radio, Stack, Stepper, Text, TextInput, Tooltip } from "@mantine/core";
import type { Puppet } from "../types";
import type { Session } from "../useSession";
import { useModels } from "../useModels";
import { restartReason, opWord, RESTART_EFFECTS } from "./RestartBox";
import { MODES, UPDATE_WARNING } from "./UpdateBox";
import { PREVIEW } from "./DestructiveBox";
import { LABELS } from "./labels";
import { TERMINAL } from "../useAdminOp";

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

/** Слово GUI для действия -- словарь #521. */
const ENTRY_WORD: Record<string, string> = {
  update: LABELS.update, model: LABELS.model, restart: LABELS.restart,
  recycle: LABELS.recycle, delete: LABELS.delete, wipe: LABELS.wipe,
};

/** Движок, отвечающий за действие (#487/#522): status живёт в
 * engineState по ключу движка. */
const ENGINE_OF: Record<string, string> = {
  update: "update", restart: "restart",
  recycle: "destructive", delete: "destructive", wipe: "destructive",
};

/** Стейт операции, поднятый движком (#522): прежний engineState нёс
 * только status -- экрану хода нужна и причина, и потерянный ответ. */
export type EngineState = { status: string; error?: string | null; lost?: boolean };

/** Слово вердикта операции на экране хода (#522): у обновления своё
 * слово успеха, остальное -- общий словарь opWord. */
const verdictWord = (act: string, op: EngineState): string =>
  act === "update" && op.status === "succeeded" ? "Обновлено" : opWord(op.status);

export function ControlWindow({ opened, onClose, onOrder, row, session,
                               initialEntry = null, engineState = {},
                               onForget }: {
  opened: boolean;
  onClose: () => void;
  /** Приказ движку: «Завершить» с финального экрана. */
  onOrder: (order: { act: string; params: Record<string, unknown> }) => void;
  row: Puppet;
  session: Session;
  /** Открыть сразу на шаге действия (#520): «новая команда» в вердикте
   * обновления возвращает к повторному выбору режима. */
  initialEntry?: ControlEntry | null;
  /** Стейт операций по движку (#487/#522): непустой статус --
   * непоказанный ход; повторный вход показывает его вердикт. */
  engineState?: Record<string, EngineState>;
  /** Забыть показанный вердикт (#522): «новая команда» из футера хода --
   * иначе pick того же пункта навсегда diverted бы на старый терминал. */
  onForget?: () => void;
}) {
  // Фаза визарда (#522 -> #527): шаги -- номера ЭКРАНОВ, у потоков их
  // разное число. Выбор действия всегда 0. Обновление (#527) -- свой
  // трёхшаговый поток: 1 -- «Что вы хотите сделать с папетом?» (радио
  // режимов с описаниями), 2 -- параметры (репозиторий у переключения;
  // «продолжить работу» шаг пропускает), 3 -- финальный экран «что
  // произойдёт», 4 -- экран хода после «Завершить». Модель -- 1
  // каталог, 3 финал; рестарт и разрушительные -- сразу 3 финал.
  // Открытие сразу на шаге действия (#520, «новая команда»): визард
  // смонтирован всегда и возвращает null закрытым, инициализатор стейта
  // срабатывает один раз -- смену initialEntry ловит prev-ref (паттерн
  // useJournal): свежий вход -- до краски, первого рендера со старым
  // шагом нет.
  const [step, setStep] = useState(initialEntry ? 1 : 0);
  const [entry, setEntry] = useState<ControlEntry | null>(initialEntry);

  // Гард #473: строка, на которую открыт визард. Смена цели после
  // выбора сбрасывает к выбору -- подтверждение не той цели умирает,
  // повтор начинается заново (паттерн useJournal: prev сверяется до
  // краски, первый рендер уже без прежнего шага).
  const flowFor = useRef<string | null>(initialEntry ? row.name : null);
  const prevEntry = useRef<ControlEntry | null | undefined>(initialEntry);
  if (prevEntry.current !== initialEntry) {
    prevEntry.current = initialEntry;
    if (initialEntry) {
      // «Новая команда» в вердикте обновления возвращает к выбору
      // режима -- свой шаг каждого потока (#527): обновление и модель
      // начинают с шага 1, прочие -- с финала
      setStep(initialEntry === "update" || initialEntry === "model" ? 1 : 3);
      setEntry(initialEntry);
      flowFor.current = row.name;
    } else { setStep(0); setEntry(null); }
  }
  const [mode, setMode] = useState<keyof typeof MODES | null>(null);
  const [origin, setOrigin] = useState("");
  const [model, setModel] = useState<string | null>(null);
  // Экран хода (#522): после «Завершить» визард остаётся и показывает
  // ход; ключ движка приходит с приказом
  const [progressOf, setProgressOf] = useState<string | null>(null);
  // Каталог моделей: спрашивается при открытом шаге модели (#468)
  const { models, error: catalogError } = useModels(
    session, session.role === "admin" && opened && entry === "model");
  // Повторный вход при непоказанном ходе (#487/#522): свежее открытие
  // (не «новая команда») с одним непустым движком -- сразу экран хода,
  // без выбора пункта. Порядок фиксирован: двух живых ходов на строку
  // не бывает (движковый дедуп), терминальный переживает один.
  const heldEngine = ["update", "restart", "destructive"].find(
    (k) => engineState[k] && engineState[k].status !== "idle") ?? null;
  const prevOpen = useRef(!opened);
  if (opened && !prevOpen.current && heldEngine && !initialEntry) {
    setProgressOf(heldEngine);
    setStep(4);
    flowFor.current = row.name;
  }
  prevOpen.current = opened;
  if (opened && step > 0 && flowFor.current !== row.name) {
    flowFor.current = null;
    setStep(0); setEntry(null); setMode(null); setOrigin(""); setModel(null);
    setProgressOf(null);
  }
  if (!opened) return null;
  const blocked = session.role === "admin" ? restartReason(row) : null;
  // Готовность к финалу: обновление -- режим (и origin для пересадки),
  // модель -- выбор из каталога, рестарт и разрушительные -- всегда.
  const ready = entry === "update"
    ? mode !== null && (mode !== "remove" || origin.trim() !== "")
    : entry === "model" ? model !== null
    : entry === "restart" || isRuin(entry);
  const params: Record<string, unknown> =
    entry === "update" && mode === "keep" ? { fresh: false }
    : entry === "update" && mode === "replace" ? { fresh: true }
    : entry === "update" && mode === "remove" && origin.trim() ? { fresh: true, origin: origin.trim() }
    : entry === "model" && model ? { model }
    : {};
  // Приказ (#522): «Завершить» шлёт приказ движку и переключает на
  // экран хода -- визард не закрывается исполнением. Модель (#488) --
  // исключение: вердикт -- балун, окно закрывается сразу.
  const finish = (act: string, p: Record<string, unknown>) => {
    onOrder({ act, params: p });
    if (act === "model") { reset(); onClose(); return; }
    setProgressOf(ENGINE_OF[act]);
    setStep(4);
  };
  // Полный сброс визарда: к первому экрану, выбор чист; показанный вердикт
  // движка забывается -- иначе pick того же пункта diverted бы обратно
  const reset = () => {
    flowFor.current = null;
    setStep(0); setEntry(null); setMode(null); setOrigin(""); setModel(null);
    setProgressOf(null);
    onForget?.();
  };
  // Пункт первого экрана (#522): выбор пункта НЕ шлёт приказ --
  // обновление раскрывает радио на месте, модель идёт к каталогу,
  // рестарт и разрушительные -- сразу на финальный экран. Повторный
  // вход при непоказанном ходе (#487): движок рапортует непустой
  // статус -- визард открывает экран хода; второго POST нет. Модель
  // (#488) исключение: её вердикт -- балун, повторный вход открывает
  // форму.
  const heldOf = (e: ControlEntry) => ENGINE_OF[e];
  const heldState = (e: ControlEntry): EngineState | null => {
    if (e === "model") return null; // балун #488 -- форме открыто
    const st = engineState[heldOf(e)];
    return st && st.status !== "idle" ? st : null;
  };
  const pick = (e: ControlEntry) => {
    const held = heldState(e);
    if (held) { setEntry(e); setProgressOf(ENGINE_OF[e]); setStep(4); return; }
    flowFor.current = row.name;
    setEntry(e);
    // #527: обновление открывает СВОЙ шаг выбора режима (1), модель --
    // каталог (1), рестарт и разрушительные -- сразу финал (3)
    setStep(e === "update" || e === "model" ? 1 : 3);
  };  // Последствия выбранного хода -- на финальный экран
  const lines = isRuin(entry) ? PREVIEW[entry].lines
    : entry === "restart" ? RESTART_EFFECTS
    : entry === "update" && mode ? MODES[mode].lines : [];
  // Экран хода (#522): стейт поднят движком по ключу progressOf
  const live = progressOf ? engineState[progressOf] : null;  const opLive = !live
    || (live.status !== "idle" && !TERMINAL.has(live.status));
  const opDone = !!live && TERMINAL.has(live.status);
  const wordOf = (st: EngineState | null) => !st ? "Отправляется"
    : progressOf === "update" ? verdictWord("update", st) : opWord(st.status);

  // ── Экран хода: Loader/вердикт, футер по терминалу ─────────────────
  // live==null -- приказ ушёл, движок ещё не отрапортовал: живой ход
  if (step === 4 && progressOf) {
    return (
      <Modal opened onClose={onClose} centered size="md" radius="md" title="правка папета"
             data-testid="control-window"
             closeButtonProps={{ "aria-label": LABELS.close }}>
        <Stack gap="md">
          <Group gap="sm" role={opLive ? "status" : undefined}>
            {opLive && <Loader size="sm" />}
            <Text size="sm" data-testid="progress-word"
                  c={opLive ? "dimmed"
                    : live?.status === "succeeded" ? "teal"
                    : live?.status === "unknown" ? "yellow" : "red"}>
              {wordOf(live)}{live?.error ? `: ${live.error}` : ""}
              {live?.lost ? " (первый ответ потерян)" : ""}
            </Text>
          </Group>
          <Group justify="flex-end" gap="sm">
            {opDone && <Button variant="light" onClick={reset}>{LABELS.again}</Button>}
            <Button variant="default" onClick={onClose}>{LABELS.close}</Button>
          </Group>
        </Stack>
      </Modal>
    );
  }

  // Шаги Степпера перерисовываются по выбранному действию (#522 ->
  // #527): обновление -- выбор/режим/параметры/финал, модель --
  // выбор/параметры/финал, остальные -- выбор/финал. Фиксированная
  // высота (#527): контейнер шага держит minHeight по максимуму
  // содержания шагов потока -- смена режима/шага не прыгает высотой.
  // Резерв считаем структурно (строка ~21px, радио ~36px, поле ~66px),
  // jsdom layout не считает -- константа потока, не измерение.
  const modelFlow = entry === "model";
  const updateFlow = entry === "update";
  const perLine = 21, perRadio = 36, perField = 66;
  const tallest = updateFlow
    ? Math.max(3 * perRadio + 2 * perLine + perField + 80,
               3 * perRadio + 5 * perLine + 80)
    : 4 * perRadio + 3 * perLine + 80;
  return (
    <Modal opened onClose={onClose} centered size="lg" radius="md" title="Правка папета"
           data-testid="control-window"
           closeButtonProps={{ "aria-label": LABELS.close }}>
      <Stepper active={updateFlow ? Math.min(step, 3)
                 : modelFlow ? Math.min(step, 2) : (step >= 3 ? 1 : 0)}
               onStepClick={setStep} allowNextStepsSelect={false}>
        <Stepper.Step label="действие" description={row.name}>
          <Stack gap="md" data-testid="wizard-body" style={{ minHeight: tallest }}>
            <Text size="sm">цель: <Text span fw={600}>{row.name}</Text></Text>
            <Stack gap="xs">
              {/* #527: обновление ведёт на СВОЙ шаг режима -- радио на
                  первом экране больше не живёт, смешения радио с
                  кнопками чужих операций нет */}
              <Button variant="light" onClick={() => pick("update")}>{LABELS.update}</Button>
              <Button variant="light" onClick={() => pick("model")}>{LABELS.model}</Button>
              <Tooltip label={blocked ?? ""} disabled={!blocked}>
                <Button variant="light" disabled={!!blocked}
                        aria-label={blocked ?? LABELS.restart}
                        onClick={() => pick("restart")}>{LABELS.restart}</Button>
              </Tooltip>
              {/* разрушительные -- пункты первого экрана (#512/#473),
                  однотонные: опасность говорит текстом финального экрана */}
              {RUIN.map((a) => (
                <Button key={a} variant="light" onClick={() => pick(a)}>
                  {a === "recycle" ? LABELS.recycle : a === "delete" ? LABELS.delete : LABELS.wipe}
                </Button>
              ))}
            </Stack>
            {/* первый экран не ведёт дальше -- ни «Назад», ни
                «Продолжить»: выбор пункта и есть переход */}
          </Stack>
        </Stepper.Step>
        {updateFlow && (
          <Stepper.Step label="режим" description={ENTRY_WORD.update}>
            {/* Шаг выбора режима (#527): заголовок тикета, радио с
                описаниями-последствиями под каждым, футер
                [Отмена][Продолжить]. Динамических полей здесь нет --
                репозиторий живёт на шаге параметров */}
            <Stack gap="md" data-testid="wizard-body" style={{ minHeight: tallest }}>
              <Text size="sm" fw={600}>Что вы хотите сделать с папетом?</Text>
              <Text size="sm">цель: <Text span fw={600}>{row.name}</Text></Text>
              <Radio.Group value={mode} onChange={(v) => setMode(v as keyof typeof MODES)}
                           data-testid="update-modes">
                <Stack gap={8} mt="xs">
                  {(Object.keys(MODES) as (keyof typeof MODES)[]).map((k) => (
                    <div key={k}>
                      <Radio value={k} label={MODES[k].label} />
                      {/* короткая подпись-описание -- у каждого радио
                          статично (#527), полные последствия -- на шаге
                          параметров, не на выборе */}
                      <Text size="sm" c="dimmed" pl={26}>{MODES[k].note}</Text>
                    </div>
                  ))}
                </Stack>
              </Radio.Group>
              <Group justify="flex-end" gap="sm">
                <Button variant="default" onClick={onClose}>{LABELS.cancel}</Button>
                {/* «продолжить работу» шаг параметров пропускает (#527):
                    у keep нет ни параметров, ни последствий -- сразу финал */}
                <Button disabled={!mode}
                        onClick={() => setStep(mode === "keep" ? 3 : 2)}>
                  {LABELS.next}
                </Button>
              </Group>
            </Stack>
          </Stepper.Step>
        )}
        {updateFlow && (
          <Stepper.Step label="параметры" description={ENTRY_WORD.update}>
            {/* Шаг параметров (#527, канон): «Что произойдёт:» с
                буллетами последствий; у переключения на другой проект --
                поле «Новый проект:» и буллет с именем цели; replace
                приходит сюда без поля. «продолжить работу» шаг
                пропускает */}
            <Stack gap="md" data-testid="wizard-body" style={{ minHeight: tallest }}>
              <Text size="sm" fw={600}>Что произойдёт:</Text>
              <Text size="sm">цель: <Text span fw={600}>{row.name}</Text></Text>
              <Text size="sm" c="dimmed">{MODES[mode ?? "remove"].label}</Text>
              {mode === "remove" && (
                <>
                  <TextInput label="Новый проект" value={origin}
                             onChange={(e) => setOrigin(e.target.value)}
                             data-testid="update-origin" />
                  <Text size="sm" c="dimmed">Имя папета останется прежним: {row.name}</Text>
                </>
              )}
              {MODES[mode ?? "remove"].lines.map((l) => (
                <Text key={l} size="sm" c="dimmed" component="li">{l}</Text>
              ))}
              <Group justify="flex-end" gap="sm">
                <Button variant="default" onClick={() => setStep(1)}>{LABELS.back}</Button>
                <Button disabled={mode === "remove" && origin.trim() === ""}
                        onClick={() => setStep(3)}>{LABELS.next}</Button>
              </Group>
            </Stack>
          </Stepper.Step>
        )}
        {modelFlow && (
          <Stepper.Step label="параметры" description={entry ? ENTRY_WORD[entry] : ""}>
            <Stack gap="md" data-testid="model-choices">
              <Text size="sm">цель: <Text span fw={600}>{row.name}</Text></Text>
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
              <Group justify="flex-end" gap="sm">
                <Button variant="default" onClick={() => setStep(0)}>{LABELS.back}</Button>
                <Button disabled={!ready} onClick={() => setStep(3)}>{LABELS.next}</Button>
              </Group>
            </Stack>
          </Stepper.Step>
        )}
        <Stepper.Step label="завершение" description={entry ? ENTRY_WORD[entry] : ""}>
          {/* Финальный экран = подтверждение (#522 -> #527): заголовок
              «Проверьте параметры операции»; у обновления факты канона
              (Папет/Узел/Текущий статус/Операция) и блок «Внимание:» у
              необратимых режимов, у прочих действий -- прежние факты и
              словарь последствий движков. «сила (force) не применяется»
              ушла (#527), «частичный исход…» упрощена */}
          <Stack gap="md" data-testid="wizard-body" style={{ minHeight: tallest }}>
            <Text size="sm" fw={600}>Проверьте параметры операции</Text>
            {updateFlow ? (<>
              {/* факты канона #527: имя, узел, статус-цитата, режим */}
              <Text size="sm">Папет: <Text span fw={600}>{row.name}</Text></Text>
              <Text size="sm">Узел: {row.node}</Text>
              <Text size="sm">Текущий статус: <Code>{row.state}</Code></Text>
              <Text size="sm">Операция: <Text span fw={600}>{mode ? MODES[mode].label : ""}</Text></Text>
              {/* «Внимание:» -- только у необратимых режимов (#527):
                  keep обратим, блока и заголовка нет */}
              {mode && mode !== "keep" && (<>
                <Text size="sm" fw={600} c="red">Внимание:</Text>
                {UPDATE_WARNING.map((l) => (
                  <Text key={l} size="sm" c="red" component="li">{l}</Text>
                ))}
              </>)}
              {mode === "remove" && (
                <Text size="sm">Новый проект: <Code>{origin.trim()}</Code></Text>
              )}
            </>) : (<>
              {/* факты точной цели -- ровно то, что на экране (#465) */}
              <Text size="sm">действие: <Text span fw={600}>{entry ? ENTRY_WORD[entry] : ""}</Text></Text>
              <Text size="sm">цель: <Text span fw={600}>{row.name}</Text></Text>
              <Text size="sm">узел: {row.node}</Text>
              <Text size="sm">состояние: <Code>{row.state}</Code></Text>
              {lines.map((l) => <Text key={l} size="sm" c="dimmed">{l}</Text>)}
              {entry === "model" && model && (
                <Text size="sm">модель: <Code>{model}</Code></Text>
              )}
              {isRuin(entry) && (
                <Text size="sm" c="red">Действие необратимо.</Text>
              )}
            </>)}
            <Text size="sm" c="dimmed">Исход показывается как есть.</Text>
            <Group justify="flex-end" gap="sm">
              <Button variant="default"
                      onClick={() => setStep(updateFlow
                        ? (mode === "keep" ? 1 : 2) : modelFlow ? 1 : 0)}>
                {LABELS.back}
              </Button>
              {/* «Завершить» = согласие (#522): финальный экран сам
                  подтверждение */}
              <Button onClick={() => { if (entry) finish(entry, params); }}>
                {LABELS.finish}
              </Button>
            </Group>
          </Stack>
        </Stepper.Step>
      </Stepper>
    </Modal>
  );
}
