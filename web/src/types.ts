// Снимок пула (#297): форма JSON, который отдают /api/pool и /events.
// Ключи закреплены сервером (mop/server/web.py, snapshot) и проверяются в
// tests/web.py; страница читает их по имени и ничего не решает второй раз:
// вид вердикта (kind), корзина (bucket) и счётчики считаются на сервере.

// Корзины -- одним списком (#323): тип и счётчики выводятся из него, а не
// перечисляются второй раз.
export const KINDS = ["free", "busy", "sick", "silent", "down"] as const;
export type Kind = (typeof KINDS)[number];

// Виды вердикта (#452) -- тем же списком, что state.KINDS на сервере:
// слово статуса и его иконка выводятся из него. null -- переходный снимок
// (сервер до #452 или спросить некого: state-строка прочерк).
export const VERDICTS = ["silent", "hung", "starting", "dialog", "login", "quota", "error",
  "waiting", "busy", "other", "free", "idle", "unknown", "failing"] as const;
export type VerdictKind = (typeof VERDICTS)[number] | null;

export type Counts = { puppets: number } & Record<Kind, number>;

// Строка папета (#452): kind -- вид вердикта (слово статуса), bucket --
// корзина (цвет), branch -- ветка чипом, detail -- подробность под словом;
// alloc_status остаётся в данных, из таблицы он ушёл в подсказку. Поля
// снимка до #452 отсутствуют -- undefined читается как null.
export interface Puppet {
  name: string; node: string; alloc_status: string; state: string;
  kind: VerdictKind; bucket?: Kind; detail?: string | null; branch?: string | null;
  owner: string; llm: string; origin: string; disk_kb: number | null;
}

export interface Project { name: string; puppets: Puppet[]; counts: Counts }

/** Корзина узла, которую считает сервер (#325, nodes.bucket). */
export type NodeKind = "free" | "busy" | "down";
/** Узел; error -- только у узла с отказом (NodeRow.to_row на сервере); kind
 *  -- корзина от сервера (#325), у снимка до #325 её нет. */
export interface Node {
  name: string; driver: string; serves: string; state: string; error?: string; kind?: NodeKind;
  free_mb: number | null; total_mb: number | null; slots: number | null; slots_total: number | null;
}

/** Токены одной строки расхода: общее у дня, папета и пользователя. */
export interface Tokens { total: number; input: number; output: number; cache_write: number; cache_read: number }
export interface UsageDay extends Tokens { date: string }
export interface UsageUser extends Tokens { login: string }
export interface PuppetUsage extends Tokens { name: string; node: string }
/** Событие журнала (#422, контракт #424): at -- исходная метка агента,
 *  received_at -- когда запись принял сервер; обычные поля события тоже
 *  здесь, credential-поля вычищены на сервере. Дальнейшие безопасные поля
 *  конверта -- optional: страница показывает их без выдумывания своих. */
export interface JournalEntry {
  at: number; received_at?: number; event: string; name: string; node: string;
  project: string; text: string;
  [extra: string]: unknown;
}
/** Строка реестра кредитов; holders -- всегда список, пустой без аренды. */
/** Сегмент полосы аллокаций (#378): корзина бегущих папетов, «прочее
 *  выделенное» (other) или свободные места (vacant) -- в порядке сервера. */
export type LoadKind = "busy" | "free" | "sick" | "silent" | "other" | "vacant";
export interface LoadSegment { kind: LoadKind; slots: number }
/** Места пула (#378, web.load): всего, выделено, свободных и сегменты
 *  полосы, сумма которых -- всего мест. Считает сервер. */
export interface Load { total: number; allocated: number; free_slots: number; segments: LoadSegment[] }

export interface Snapshot {
  at: number | null;
  projects: Project[];
  counts: Counts;
  nodes: Node[];
  usage: UsageDay[];
  per_puppet: PuppetUsage[];
  per_user: UsageUser[];
  /** Окно раскатки #424: журнал ушёл из публичного снимка; старый сервер
   *  ещё кладёт его сюда -- главная его больше не читает. */
  journal?: JournalEntry[];
  errors: string[];
  masters: Master[];
  /** период опроса who в секундах (#325, MASTERS_EVERY) */
  masters_every?: number;
  /** места пула и полоса аллокаций (#378); у снимка до #378 его нет --
   *  окно раскатки, когда web/dist новый, а сервер ещё старый */
  load?: Load;
}

/** Живой мастер по опросу who (#305, #421, #423): строка -- живая
 *  Claude-сессия. master -- первый адрес для send, addresses -- все адреса
 *  сессии; identified -- серверный маркер: строка склеена по sessionId
 *  (сырой id публично не ходит), false -- legacy endpoint без личности.
 *  Старый снимок без addresses/identified читается как один адрес и false. */
export interface Master {
  project: string;
  master: string;
  addresses?: string[];
  identified?: boolean;
  user: string;
  session: string;
  cwd: string;
  puppets: string[];
}
