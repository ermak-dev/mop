// Канал вердиктов операций (#504): терминал приезжает кадром SSE
// `operation` из существующего потока /events (#424/#499), опрос статуса
// операции уходит -- ровно один GET сразу после 202 ловит мгновенные
// refused и показывает waiting/running на момент подачи, дальше вердикт
// ждут кадром. Канал -- модульная шина: соединение держит App (один
// EventSource на страницу), подписчики -- строки таблицы (unknown-
// карточка цели) и операции по id (useAdminOp), таймер потерянного
// вердикта кормится и кадром, и тем единственным GET.
//
// Таймер (#504): операция, принятая этой страницей (id из 202 есть),
// чей терминальный кадр не пришёл к сроку, честно бейджится unknown
// «исход неизвестен, смотри журнал». Срок -- не браузерный произвол:
// серверный потолок любой операции, таймаут эффекта; его дефолт 900 с,
// запас 60. Пришедший кадр unknown (как и любой терминал) рисуется
// сразу, без анти-мигания. «202 не долетел» не отслеживается вовсе:
// без id нет ни операции, ни таймера.
import { useEffect, useState } from "react";

/** Срок потерянного вердикта: серверный MOP_WEB_EFFECT_TIMEOUT, дефолт;
 * расхождение при переопределении на установке -- известный остаток. */
export const VERDICT_FALLBACK_MS = (900 + 60) * 1000;

export interface TargetVerdict {
  /** терминал #476 или промежуточный из единственного GET. */
  status: string;
  /** id операции -- дедуп дублей кадра и опознание своего. */
  id: string | null;
  /** причина-фраза сервера (allowlist), у unknown -- нет. */
  error: string | null;
  /** true -- терминал не пришёл к сроку: вердикт поставил таймер. */
  lostVerdict: boolean;
  /** что за операция: слово карточки/балуна. */
  action: string | null;
}

export interface OperationFrame {
  id: string;
  action: string;
  target: string | null;
  status: string;
  error?: string;
}

/** Разобрать кадр operation; мусор -- null, не бросок. */
export function parseFrame(raw: string): OperationFrame | null {
  try {
    const f = JSON.parse(raw) as Partial<OperationFrame>;
    if (typeof f.id !== "string" || typeof f.status !== "string") return null;
    return { id: f.id, action: typeof f.action === "string" ? f.action : "",
             target: typeof f.target === "string" ? f.target : null,
             status: f.status,
             error: typeof f.error === "string" ? f.error : undefined };
  } catch {
    return null;
  }
}

type FrameListener = (frame: OperationFrame) => void;

// Шина канала: одно соединение (App), много подписчиков (операции по id,
// строка таблицы). Термinals only -- промежуточных кадров сервер не шлёт.
const TERMINAL = new Set(["succeeded", "refused", "failed", "partial", "unknown"]);

class OperationBus {
  private listeners = new Set<FrameListener>();
  /** Вердикты целей: слово для строки таблицы (#504). */
  verdicts: Record<string, TargetVerdict> = {};
  private byId = new Map<string, TargetVerdict>();
  private pending = new Map<string, { id: string; timer: number }>();
  // Таймеры бесцельных операций (add, #470): строки-цели нет, операция одна.
  private pendingIds = new Map<string, number>();
  private sink: ((all: Record<string, TargetVerdict>) => void) | null = null;

  /** Кадр из SSE-потока: терминал -- подписчикам и на цель. */
  frame(frame: OperationFrame) {
    if (!TERMINAL.has(frame.status)) return;
    for (const l of [...this.listeners]) l(frame);
    if (!frame.target) {
      // Бесцельной операции (-- add) вердикт пришёл: её таймер снят.
      const t = this.pendingIds.get(frame.id);
      if (t !== undefined) { clearTimeout(t); this.pendingIds.delete(frame.id); }
      return;
    }
    // Дубль/опоздание кадра того же id не перезаписывает уже стоящий
    // вердикт цели: терминал у операции один, второе слово -- артефакт.
    if (this.byId.has(frame.id)) return;
    this.byId.set(frame.id, this.verdict(frame, false));
    this.land(frame.target, this.verdict(frame, false), frame.id);
  }

  /** Единственный GET после 202 (useAdminOp): статусы подачи и мгновенный
   * терминал; терминал снимает таймер, промежуточный -- только слово. */
  observed(target: string | null, id: string, status: string,
           error: string | null) {
    if (!TERMINAL.has(status)) {
      if (target && status) {
        this.verdicts = { ...this.verdicts,
                          [target]: this.verdict({ id, action: "", target,
                                                   status }, false) };
        this.sink?.(this.verdicts);
      }
      return;
    }
    if (!target) {
      // Единственный GET принёс терминал бесцельной операции: таймер снят.
      const t = this.pendingIds.get(id);
      if (t !== undefined) { clearTimeout(t); this.pendingIds.delete(id); }
      return;
    }
    if (this.byId.has(id)) return;
    this.byId.set(id, this.verdict({ id, action: "", target, status,
                                     error: error ?? undefined }, false));
    this.land(target, this.verdict({ id, action: "", target, status,
                                    error: error ?? undefined }, false), id);
  }

  /** Ответ 202 долетел (#504): у операции есть id -- завести таймер
   * потерянного вердикта. Повторный клик с тем же id таймер не сдвигает
   * (идемпотентность сервера даёт тот же id). Бесцельной (add, #470) строки
   * нет -- таймер кормит только подписку по id. */
  accepted(target: string | null, id: string) {
    if (!target) {
      if (this.pendingIds.has(id)) return;
      const timer = window.setTimeout(() => {
        this.pendingIds.delete(id);
        for (const l of [...this.listeners])
          l({ id, action: "", target: null, status: "unknown" });
      }, VERDICT_FALLBACK_MS);
      this.pendingIds.set(id, timer);
      return;
    }
    const known = this.pending.get(target);
    if (known && known.id === id) return;
    if (known) clearTimeout(known.timer);
    const timer = window.setTimeout(() => {
      this.pending.delete(target);
      const v: TargetVerdict = { status: "unknown", id, error: null,
                                 lostVerdict: true, action: null };
      this.verdicts = { ...this.verdicts, [target]: v };
      this.sink?.(this.verdicts);
      for (const l of [...this.listeners])
        l({ id, action: "", target, status: "unknown" });
    }, VERDICT_FALLBACK_MS);
    this.pending.set(target, { id, timer });
  }

  /** Запись вытекла («not found»): правда -- журнал, это unknown. */
  expired(target: string | null, id: string) {
    if (!target) {
      // Единственный GET принёс «not found» бесцельной операции: таймер снят.
      const t = this.pendingIds.get(id);
      if (t !== undefined) { clearTimeout(t); this.pendingIds.delete(id); }
      return;
    }
    if (this.byId.has(id)) return;
    this.byId.set(id, { status: "unknown", id, error: null,
                        lostVerdict: false, action: null });
    this.land(target, { status: "unknown", id, error: null,
                        lostVerdict: false, action: null }, id);
  }

  private verdict(f: OperationFrame, lostVerdict: boolean): TargetVerdict {
    return { status: f.status, id: f.id, error: f.error ?? null,
             lostVerdict, action: f.action || null };
  }

  private land(target: string, v: TargetVerdict, id: string) {
    const wait = this.pending.get(target);
    if (wait && wait.id === id) {
      clearTimeout(wait.timer);
      this.pending.delete(target);
    }
    this.verdicts = { ...this.verdicts, [target]: v };
    this.sink?.(this.verdicts);
  }

  subscribe(l: FrameListener): () => void {
    this.listeners.add(l);
    return () => { this.listeners.delete(l); };
  }

  /** Проверки: чистый словарь между случаями (шина модульная). */
  resetForTest() {
    this.listeners.clear();
    this.verdicts = {};
    this.byId.clear();
    for (const w of this.pending.values()) clearTimeout(w.timer);
    this.pending.clear();
    for (const t of this.pendingIds.values()) clearTimeout(t);
    this.pendingIds.clear();
    this.sink = null;
  }

  /** Строка таблицы слушает словарь целей (#504): подписка на слово. */
  watch(sink: (all: Record<string, TargetVerdict>) => void): () => void {
    this.sink = sink;
    sink(this.verdicts);
    return () => { this.sink = null; };
  }
}

/** Канал страницы: один на модуль, App подключает поток. */
export const operations = new OperationBus();

/** Реакция на кадры SSE: подключается App к своему EventSource /events
 * (#504) -- второго соединения страница не заводит. */
export function onOperationEvent(e: MessageEvent) {
  const frame = parseFrame(e.data);
  if (frame) operations.frame(frame);
}

/** Вердикты целей для строки таблицы: слово по цели (#504). */
export function useTargetVerdicts(): Record<string, TargetVerdict> {
  const [all, setAll] = useState<Record<string, TargetVerdict>>({});
  useEffect(() => operations.watch(setAll), []);
  return all;
}
