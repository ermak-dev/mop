// Снимок пула (#297): форма JSON, который отдают /api/pool и /events.
// Ключи закреплены сервером (mop/server/web.py, snapshot) и проверяются в
// tests/web.py; страница читает их по имени и ничего не решает второй раз:
// корзина папета (kind) и счётчики считаются на сервере.

// Корзины -- одним списком (#323): тип и счётчики выводятся из него, а не
// перечисляются второй раз.
export const KINDS = ["free", "busy", "sick", "silent", "down"] as const;
export type Kind = (typeof KINDS)[number];

export type Counts = { puppets: number } & Record<Kind, number>;

export interface Puppet {
  name: string; node: string; alloc_status: string; state: string; kind: Kind;
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
/** Событие журнала: метку at ставит сборщик каждому (Collector.event). */
export interface JournalEntry { at: number; event: string; name: string; node: string; project: string; text: string }
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
  journal: JournalEntry[];
  errors: string[];
  masters: Master[];
  /** период опроса who в секундах (#325, MASTERS_EVERY) */
  masters_every?: number;
  /** места пула и полоса аллокаций (#378); у снимка до #378 его нет --
   *  окно раскатки, когда web/dist новый, а сервер ещё старый */
  load?: Load;
}

/** Живой мастер по опросу who (#305): адрес для send, логин, сессия, каталог
 *  и папеты проекта, чья аренда на этом логине. */
export interface Master {
  project: string;
  master: string;
  user: string;
  session: string;
  cwd: string;
  puppets: string[];
}
