import type { Master, Project, RosterSession } from "./types";

/** Старый снимок не даёт идентичности сверх адреса; имя и cwd не ключи.
 *  Пустой sessions — действительный пустой ростер, не повод брать проекции. */
export function roster(projects: Project[], sessions?: RosterSession[], masters: Master[] = []): RosterSession[] {
  if (Array.isArray(sessions)) return sessions;
  return [
    ...projects.flatMap((project) => project.puppets.map((puppet) => ({
      ...puppet,
      source: "managed" as const,
      id: JSON.stringify(["managed", project.name, puppet.name, puppet.soul_inc ?? null]),
      project: project.name,
      user: puppet.owner && puppet.owner !== "-" ? puppet.owner : null,
      role: puppet.soul ?? null,
      role_source: "meta" as const,
      incarnation: puppet.soul_inc ?? null,
      soul_digest: puppet.soul_digest ?? null,
      unmanaged: false as const,
      conversation: null,
      addresses: [puppet.name],
    }))),
    ...masters.map((master) => ({
      source: "local" as const,
      id: JSON.stringify(["local", master.project, master.master]),
      project: master.project,
      user: master.user && master.user !== "-" ? master.user : null,
      role: null,
      role_source: "legacy" as const,
      unmanaged: true as const,
      conversation: master.session || null,
      display: master.session || master.master,
      cwd: master.cwd || null,
      addresses: master.addresses?.length ? master.addresses : [master.master],
      identified: master.identified === true,
    })),
  ];
}

export function roleKey(entry: RosterSession): string {
  if (entry.source === "local") return "local";
  return entry.role ? `role:${entry.role}` : "unassigned";
}

export function roleLabel(key: string): string {
  if (key === "local") return "Legacy local";
  if (key === "unassigned") return "Unassigned";
  return key.slice("role:".length);
}
