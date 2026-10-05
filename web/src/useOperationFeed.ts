// Канал вердиктов операций (#504): терминал приезжает кадром SSE
// `operation` из существующего потока /events (#424/#499), опрос статуса
// операции уходит -- ровно один GET сразу после 202 ловит мгновенные
// refused и показывает waiting/running на момент подачи, дальше вердикт
// ждут кадром. Канал -- модульная шина: соединение держит App (один
// EventSource на страницу), подписчики -- операции по id (useAdminOp) и
// балун терминального кадра (useOperationBalloons, #509), таймер
// потерянного вердикта кормится и кадром, и тем единственным GET.
// Словарь {цель -> исход} ушёл вместе с карточкой строки (#509):
// терминал возвращается человеку балуном, а не словом в таблице.
//
// Таймер (#504): операция, принятая этой страницей (id из 202 есть),
// чей терминальный кадр не пришёл к сроку, честно доходит unknown до
// своего бокса. Срок -- не браузерный произвол: серверный потолок любой
// операции, таймаут эффекта; его дефолт 900 с, запас 60. Пришедший кадр
// unknown (как и любой терминал) уходит подписчикам сразу, без
// анти-мигания. «202 не долетел» не отслеживается вовсе: без id нет ни
// операции, ни таймера.

/** Срок потерянного вердикта: серверный MOP_WEB_EFFECT_TIMEOUT, дефолт;
 * расхождение при переопределении на установке -- известный остаток. */
export const VERDICT_FALLBACK_MS = (900 + 60) * 1000;

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
// балун кадра #509). Терминалы only -- промежуточных кадров сервер не шлёт.
const TERMINAL = new Set(["succeeded", "refused", "failed", "partial", "unknown"]);

class OperationBus {
  private listeners = new Set<FrameListener>();
  /** Свои id -- из 202 этой страницы (#509): вердикт своего хода покажет
   * бокс операции, балун кадра его не дублирует (один клик -- один балун). */
  private own = new Set<string>();
  /** Таймеры целей: свой терминал не пришёл к сроку -- unknown по id. */
  private pending = new Map<string, { id: string; timer: number }>();
  // Таймеры бесцельных операций (add, #470): строки-цели нет, операция одна.
  private pendingIds = new Map<string, number>();

  /** Кадр из SSE-потока: терминал -- подписчикам; свой таймер цели снят. */
  frame(frame: OperationFrame) {
    if (!TERMINAL.has(frame.status)) return;
    for (const l of [...this.listeners]) l(frame);
    if (!frame.target) {
      // Бесцельной операции (-- add) вердикт пришёл: её таймер снят.
      const t = this.pendingIds.get(frame.id);
      if (t !== undefined) { clearTimeout(t); this.pendingIds.delete(frame.id); }
      return;
    }
    this.settle(frame.target, frame.id);
  }

  /** Единственный GET после 202 (useAdminOp): мгновенный терминал снимает
   * таймер цели; промежуточные состояния живут в боксе, канал молчит. */
  observed(target: string | null, id: string, status: string) {
    if (!TERMINAL.has(status)) return;
    if (!target) {
      // Единственный GET принёс терминал бесцельной операции: таймер снят.
      const t = this.pendingIds.get(id);
      if (t !== undefined) { clearTimeout(t); this.pendingIds.delete(id); }
      return;
    }
    this.settle(target, id);
  }

  /** Ответ 202 долетел (#504): у операции есть id -- завести таймер
   * потерянного вердикта и пометить id своим (#509). Повторный клик с тем
   * же id таймер не сдвигает (идемпотентность сервера даёт тот же id).
   * Бесцельной (add, #470) строки нет -- таймер кормит только подписку по
   * id. */
  accepted(target: string | null, id: string) {
    this.own.add(id);
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
      for (const l of [...this.listeners])
        l({ id, action: "", target, status: "unknown" });
    }, VERDICT_FALLBACK_MS);
    this.pending.set(target, { id, timer });
  }

  /** Запись вытекла («not found»): правда -- журнал, это unknown в боксе;
   * таймер снят, второго слова не будет. */
  expired(target: string | null, id: string) {
    if (!target) {
      // Единственный GET принёс «not found» бесцельной операции: таймер снят.
      const t = this.pendingIds.get(id);
      if (t !== undefined) { clearTimeout(t); this.pendingIds.delete(id); }
      return;
    }
    this.settle(target, id);
  }

  /** Свой ли id (#509): балун кадра дублирует вердикт своего бокса. */
  owns(id: string): boolean {
    return this.own.has(id);
  }

  /** Терминал дошёл до своей цели: таймер потерянного вердикта снят. */
  private settle(target: string, id: string) {
    const wait = this.pending.get(target);
    if (wait && wait.id === id) {
      clearTimeout(wait.timer);
      this.pending.delete(target);
    }
  }

  subscribe(l: FrameListener): () => void {
    this.listeners.add(l);
    return () => { this.listeners.delete(l); };
  }

  /** Проверки: чистое состояние между случаями (шина модульная). */
  resetForTest() {
    this.listeners.clear();
    this.own.clear();
    for (const w of this.pending.values()) clearTimeout(w.timer);
    this.pending.clear();
    for (const t of this.pendingIds.values()) clearTimeout(t);
    this.pendingIds.clear();
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
