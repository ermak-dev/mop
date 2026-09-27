// Снимок пула (#297): форма JSON, который отдают /api/pool и /events.
// Ключи закреплены сервером (mop/server/web.py, snapshot) и проверяются в
// tests/web.py; страница читает их по имени и ничего не решает второй раз:
// корзина папета (kind) и счётчики считаются на сервере.

export type Kind = "free" | "busy" | "sick" | "silent" | "down";
export const KINDS: Kind[] = ["free", "busy", "sick", "silent", "down"];

export interface Counts { puppets: number; free: number; busy: number; sick: number; silent: number; down: number }

export interface Puppet {
  name: string; node: string; alloc_status: string; state: string; kind: Kind;
  owner: string; llm: string; origin: string; disk_kb: number | null;
}

export interface Project { name: string; puppets: Puppet[]; counts: Counts }

export interface Node {
  name: string; driver: string; serves: string; state: string;
  free_mb: number | null; total_mb: number | null; slots: number | null; slots_total: number | null;
}

export interface UsageDay { date: string; total: number; input: number; output: number; cache_write: number; cache_read: number }
export interface UsageUser { login: string; total: number; input: number; output: number; cache_write: number; cache_read: number }
export interface JournalEntry { at?: number; time?: string; event: string; name: string; node: string; project: string; text: string }
export interface Cred {
  name: string; profile: string; kind: string; owner: string; status: string;
  resets_at: number | null; percent: number | null; age: string; holders?: string[];
}

export interface Snapshot {
  at: number | null;
  projects: Project[];
  counts: Counts;
  nodes: Node[];
  usage: UsageDay[];
  per_puppet: Record<string, unknown>;
  per_user: UsageUser[];
  journal: JournalEntry[];
  errors: string[];
  creds: Cred[];
}
