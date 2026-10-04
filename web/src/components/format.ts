// Общее для компонентов (#298): та же запись чисел и корзин, что была на
// старой странице, чтобы перенос не менял вид. Корзину (kind) считает
// сервер (mop/server/web.py), здесь только её имя и цвет для человека.
//
// Одно место на запись (#323): склонение, короткие числа, слова корзин и
// цвет статуса кредита жили копиями в App, usage-format и creds-api.
import { KINDS, type Counts, type Kind, type VerdictKind } from "../types";

// Слова корзин: one -- у одного папета (подпись проекта), many -- у
// счётчика в шапке. Слова прежние, как были в двух таблицах.
export const KIND_WORD: Record<Kind, { one: string; many: string }> = {
  free: { one: "свободен", many: "свободны" },
  busy: { one: "занят", many: "заняты" },
  sick: { one: "болен", many: "больны" },
  silent: { one: "агент молчит", many: "агент молчит" },
  down: { one: "не работает", many: "не подняты" },
};
// Цвета старой страницы: зелёный, янтарный, красный, фиолетовый, серый.
export const KIND_COLOR: Record<Kind, string> = {
  free: "green", busy: "yellow", sick: "red", silent: "violet", down: "gray",
};

// #452: слово статуса -- по виду вердикта (kind), тем же словам, что
// оператор читал в state-строке, одним словом. Список -- те же VERDICTS,
// что на сервере; незнакомого вида страница не рисует.
export const VERDICT_WORD: Record<Exclude<VerdictKind, null>, string> = {
  silent: "агент молчит", hung: "завис", dialog: "ждёт действия",
  login: "логин", quota: "нет квоты", error: "ошибка", waiting: "ждёт ввода",
  busy: "занят", other: "иначе", free: "свободен", idle: "работа в клоне",
  unknown: "неизвестно", failing: "падает",
};

/** Слово статуса одним словом (#452): вид из снимка, null -- «нет данных». */
export const verdictWord = (kind: VerdictKind): string =>
  (kind && VERDICT_WORD[kind]) || "нет данных";

/** Цвет строки статуса (#452): по корзине из снимка, а не по виду --
 *  больной и молчащий красятся своей краской, остальное -- корзиной;
 *  корзины нет (переходный снимок) -- серый, как down. */
export const verdictColor = (kind: VerdictKind, bucket: Kind | undefined | null): string =>
  (kind === "silent" && KIND_COLOR.silent) || KIND_COLOR[bucket || "down"];

/** Корзины, в которых кто-то есть, в порядке KINDS. */
export function presentKinds(counts: Counts): Kind[] {
  return KINDS.filter((k) => counts[k]);
}

/** Склонение по числу: 1 папет, 3 папета, 11 папетов. */
export function plural(n: number, one: string, few: string, many: string): string {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return `${n} ${one}`;
  if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return `${n} ${few}`;
  return `${n} ${many}`;
}

/** Список через запятую, "-" для пустого. */
export function listOrDash(items: string[]): string {
  return items.length ? items.join(", ") : "-";
}

/** Место клона: КБ -> "N GB" | "N MB", "-" без данных. */
export function gb(kb: number | null | undefined): string {
  if (kb == null) return "-";
  return kb >= 1048576 ? `${(kb / 1048576).toFixed(0)} GB` : `${(kb / 1024).toFixed(0)} MB`;
}

/** МБ -> "N GB" вниз, "-" без данных. Вниз, как `mop node` и строка пула
 *  (#329): свободного не бывает больше, чем есть -- 1536 МБ это «1 GB». */
export function gbOfMb(mb: number | null | undefined): string {
  return mb == null ? "-" : `${Math.floor(mb / 1024)} GB`;
}

/** Токены коротко: 1.2k, 15M, 2.3G -- меньше десяти единиц с одним знаком. */
export function human(n: number): string {
  for (const [u, d] of [["G", 1e9], ["M", 1e6], ["k", 1e3]] as const) {
    if (n >= d) { const v = n / d; return (v < 10 ? v.toFixed(1) : v.toFixed(0)) + u; }
  }
  return String(n);
}

/** Та же запись, что render.ratio в питоне (#243): "свободно/всего". */
export function ratio(free: number | null | undefined, total: number | null | undefined): string {
  if (free == null && total == null) return "-";
  return `${free == null ? "-" : free}/${total == null ? "-" : total}`;
}

/** Время события журнала: чч:мм:сс по часам браузера, "-" без метки. */
export function hhmmss(ts: number | null | undefined): string {
  return ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "-";
}

export const percentText = (p: number | null) => (p == null ? "-" : `${p}%`);

// Время сброса квоты (#331) -- в поясе смотрящего. Сервер прежде вписывал
// его в слово статуса сам: «%H:%M:%S» в поясе сервера и без даты, и недельное
// окно через три дня читалось как «сегодня в пять утра». Теперь слово голое,
// а resets_at (эпоха, секунды) пишется здесь: «до чч:мм» сегодня и
// «до дд.мм чч:мм» в другой день; секунды не нужны. timeZone -- для проверок,
// страница его не передаёт, и пояс -- браузера.
function wallClock(ts: number, timeZone?: string): Record<string, string> {
  const fmt = new Intl.DateTimeFormat("ru-RU", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });
  return Object.fromEntries(fmt.formatToParts(new Date(ts * 1000)).map((p) => [p.type, p.value]));
}

/** «до чч:мм» или «до дд.мм чч:мм»; "" без времени сброса. */
export function untilText(resetsAt: number | null, now: number = Date.now() / 1000, timeZone?: string): string {
  if (resetsAt == null) return "";
  const r = wallClock(resetsAt, timeZone), n = wallClock(now, timeZone);
  const today = r.year === n.year && r.month === n.month && r.day === n.day;
  return today ? `до ${r.hour}:${r.minute}` : `до ${r.day}.${r.month} ${r.hour}:${r.minute}`;
}

/** Статус кредита для ячейки: к quota_wait -- время сброса (#326: по виду,
 *  не по слову). Снимок без вида (сервер до #325, у которого до #331 время
 *  уже в слове) -- слово как есть. */
