// Shared fetch helper that surfaces failures as toasts instead of silently
// returning empty/null. Every panel using fetch() should go through this so
// CORS / network / 500 errors are visible immediately.

import type { ToastApi } from "../components/Toast";

export const API_BASE = "http://127.0.0.1:8765";

export async function fetchJson<T>(
  path: string,
  toast: ToastApi | null,
  opts?: RequestInit,
): Promise<T | null> {
  const url = path.startsWith("http") ? path : `${API_BASE}${path}`;
  try {
    const r = await fetch(url, opts);
    if (!r.ok) {
      const body = await r.text().catch(() => "");
      toast?.show(`HTTP ${r.status}: ${path}${body ? " — " + body.slice(0, 100) : ""}`, {
        variant: "error",
        duration: 5000,
      });
      console.error(`fetchJson ${r.status}`, url, body);
      return null;
    }
    return (await r.json()) as T;
  } catch (e) {
    const msg = (e as Error).message;
    toast?.show(`Network error: ${msg}`, { variant: "error", duration: 5000 });
    console.error("fetchJson failed", url, e);
    return null;
  }
}
