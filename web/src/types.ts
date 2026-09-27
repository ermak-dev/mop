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

/** Узел; error -- только у узла с отказом (NodeRow.to_row на сервере). */
export interface Node {
  name: string; driver: string; serves: string; state: string; error?: string;
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
export interface Cred {
  name: string; profile: string; kind: string; owner: string; status: string;
  resets_at: number | null; percent: number | null; age: string; holders: string[];
}

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
  creds: Cred[];
  masters: Master[];
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
