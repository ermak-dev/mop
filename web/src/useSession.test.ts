// Сессия дашборда (#416): контракт #415 -- GET /api/session, POST /api/login,
// POST /api/logout. Формы запросов и разбор ответов, fetch подменён.
import { afterEach, expect, test, vi } from "vitest";
import { fetchSession, GUEST, LoginRefused, postLogin, postLogout } from "./useSession";

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

afterEach(() => vi.unstubAllGlobals());

test("#416 GET /api/session: guest and a logged-in operator", async () => {
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(json(200, { login: null, role: null }))
    .mockResolvedValueOnce(json(200, { login: "anton", role: "admin" }));
  vi.stubGlobal("fetch", fetchMock);
  expect(await fetchSession()).toEqual(GUEST);
  expect(await fetchSession()).toEqual({ login: "anton", role: "admin" });
  const [url, init] = fetchMock.mock.calls[0];
  expect(url).toBe("/api/session");
  expect(init.credentials).toBe("same-origin");
  expect(init.method).toBeUndefined(); // GET без тела -- без Content-Type
  expect(init.headers).toBeUndefined();
});

test("#416 GET /api/session 5xx is the panel's error, not a guest", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("boom", { status: 502 })));
  await expect(fetchSession()).rejects.toThrow();
});

test("#416 POST /api/login: JSON body, same-origin cookies; 200 -> session", async () => {
  const fetchMock = vi.fn().mockResolvedValue(json(200, { login: "anton", role: "user" }));
  vi.stubGlobal("fetch", fetchMock);
  expect(await postLogin("anton", "secret")).toEqual({ login: "anton", role: "user" });
  const [url, init] = fetchMock.mock.calls[0];
  expect(url).toBe("/api/login");
  expect(init.method).toBe("POST");
  expect(init.credentials).toBe("same-origin");
  expect(init.headers).toEqual({ "Content-Type": "application/json" });
  expect(JSON.parse(init.body)).toEqual({ login: "anton", password: "secret" });
});

test("#416 POST /api/login 401 carries the server's reason, 429 its own", async () => {
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(json(401, { error: "invalid login or password" }))
    .mockResolvedValueOnce(new Response(null, { status: 429 })));
  const asRefused = async (login: string, password: string): Promise<LoginRefused> => {
    try { await postLogin(login, password); throw new Error("expected a refusal"); }
    catch (e) { expect(e).toBeInstanceOf(LoginRefused); return e as LoginRefused; }
  };
  expect((await asRefused("x", "y")).status).toBe(401);
  expect((await asRefused("x", "y")).status).toBe(429);
});

test("#416 POST /api/logout: guest object back", async () => {
  const fetchMock = vi.fn().mockResolvedValue(json(200, { login: null, role: null }));
  vi.stubGlobal("fetch", fetchMock);
  expect(await postLogout()).toEqual(GUEST);
  const [url, init] = fetchMock.mock.calls[0];
  expect(url).toBe("/api/logout");
  expect(init.method).toBe("POST");
  expect(init.credentials).toBe("same-origin");
  expect(init.headers).toEqual({ "Content-Type": "application/json" });
});
