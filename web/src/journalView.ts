// Представление события журнала (#422): та же запись, что отдал сервер
// (journal_entry, #424 расширяет конверт), -- здесь только слова для
// человека. Хелперы не знают, откуда запись пришла: главная, страница
// журнала и диалог читают одни и те же строки.
import type { JournalEntry } from "./types";

/** text как строка: сервер (full_entry) сохраняет поле каким угодно JSON
 *  и может вовсе его не положить. Число -- показать числом, отсутствие --
 *  пустой строкой; сырое значение без потерь остаётся в модале (orDash). */
function asText(v: unknown): string {
  if (v == null) return "";
  return typeof v === "string" ? v : String(v);
}

/** Полная дата события: дд.мм.гггг чч:мм:сс + пояс смотрящего, «-» без
 *  метки. Главная пишет одни чч:мм:сс (format.hhmmss) -- журнал живёт
 *  дольше дня, и без даты утреннее событие не отличить от вчерашнего, а
 *  без пояса -- чей это день (#422, ревью 2: пояс обязателен в записи).
 *  timeZone -- для проверок, страница его не передаёт. */
export function fullAt(ts: number | null | undefined, timeZone?: string): string {
  if (!ts) return "-";
  const date = new Date(ts * 1000);
  // конечная, но вне диапазона Date метка (бэкенд допускает 1e20) --
  // прочерк, а не RangeError, роняющий всю страницу журнала (#422, ревью 3)
  if (isNaN(date.getTime())) return "-";
  const fmt = new Intl.DateTimeFormat("ru-RU", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
    timeZoneName: "shortOffset",
  });
  const get = (t: string) => fmt.formatToParts(date).find((x) => x.type === t)?.value ?? "";
  // node печатает «GMT» вместо «UTC» -- это один и тот же нулевой пояс,
  // приводим к каноническому виду, чтобы запись не зависела от среды
  const zone = get("timeZoneName").replace(/^GMT$/, "UTC");
  return `${get("day")}.${get("month")}.${get("year")} ${get("hour")}:${get("minute")}:${get("second")} ${zone}`;
}

/** Заголовок карточки: «событие — папет»; без папета («up» пишет агент
 *  узла) -- одно событие. */
export function entryTitle(e: JournalEntry): string {
  return e.name && e.name !== "-" ? `${e.event} — ${e.name}` : e.event;
}

/** Осмысленное описание по виду события (#422): не сырой text, а фраза.
 *  Виды -- те, что публикует агент (_event): send, idle, wipe, type, up.
 *  Незнакомый вид не выдумывает своей фразы -- остаётся text как есть. */
export function journalText(e: JournalEntry): string {
  // text нормализован в строку: сервер (full_entry) кладёт какое угодно
  // значение и может не положить поля вовсе -- фраза не имеет права крашить
  const text = asText(e.text);
  switch (e.event) {
    // агент пишет «from <имя>» -- фраза по-русски, имя остаётся как есть;
    // пустое или отсутствующее имя -- фраза без хвоста, не «сообщение от»
    case "send": {
      const who = text.replace(/^from\s+/, "").trim();
      return who ? `сообщение от ${who}` : "сообщение";
    }
    case "idle": return text ? `дождался простоя: ${text}` : "дождался простоя";
    case "wipe": return "клон снесён";
    case "type": return text ? `команда в клоне: ${text}` : "команда в клоне";
    // «up» агент пишет латиницей «agent on <узел>» -- переводим узел в фразу
    case "up": {
      const node = text.replace(/^agent on\s+/, "");
      return node && node !== text ? `агент на узле ${node} поднялся` : "агент поднялся";
    }
    default: return text;
  }
}
