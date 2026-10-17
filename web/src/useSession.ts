// Django session общая с Admin; пароль/токен не сохраняются клиентом.
import { hybridRequest, HybridHttpError } from './hybridApi';
export interface Session { login: string | null; role: 'admin' | 'user' | null; generation?: string; authenticated?: boolean }
export const GUEST: Session = { login: null, role: null };
// Контролы лишь предлагают команду. Текущие права и ownership решает DRF;
// identity не публикует is_staff/permissions, поэтому admin не выдумывается.
export const hasControls = (session: Session) => session.authenticated === true || session.role === 'admin';
export class LoginRefused extends Error {
  constructor(public status: number, public reason: string) { super(reason); }
}
let generation = 0;
let lastCSRF = '';
function currentGeneration() {
  const csrf = document.cookie.split(';').find(item => item.trim().startsWith('csrftoken=')) ?? '';
  if (csrf !== lastCSRF) { lastCSRF = csrf; generation++; }
  return String(generation);
}
export async function fetchSession(): Promise<Session> {
  try {
    const value = await hybridRequest<{ schema_version: number; user_id: number; principal_id: string; kind: string }>('/api/v1/identity');
    if (value.schema_version !== 1 || !Number.isSafeInteger(value.user_id) || value.kind !== 'human') throw new Error('Unsupported browser identity');
    return { login: String(value.user_id), role: 'user', authenticated: true, generation: currentGeneration() };
  } catch (error) {
    if (error instanceof HybridHttpError && [401, 403].includes(error.status) && ['not_authenticated', 'authentication_failed'].includes(error.code ?? '')) return GUEST;
    throw error;
  }
}
export async function postLogin(login: string, password: string): Promise<Session> {
  try {
    await hybridRequest('/api/v1/auth/csrf');
    await hybridRequest('/api/v1/auth/login', { username: login, password });
    generation++;
    return await fetchSession();
  } catch (error) {
    if (error instanceof HybridHttpError) throw new LoginRefused(error.status, error.message);
    throw error;
  }
}
export async function postLogout(): Promise<Session> {
  await hybridRequest('/api/v1/auth/logout', {});
  generation++;
  return GUEST;
}
