// IMPROVEMENTS #18: pop the chart into a standalone window for second monitor.

import { WebviewWindow } from "@tauri-apps/api/webviewWindow";

const POPOUT_LABEL = "chart-popout";

export async function popoutChart(): Promise<void> {
  try {
    // If the window already exists, just focus it
    const existing = await WebviewWindow.getByLabel(POPOUT_LABEL);
    if (existing) {
      await existing.setFocus();
      return;
    }
  } catch {
    // ignore — getByLabel may throw if window doesn't exist
  }

  const popout = new WebviewWindow(POPOUT_LABEL, {
    url: "/?view=chart-only",
    title: "FEF Chart",
    width: 1400,
    height: 900,
    minWidth: 800,
    minHeight: 500,
  });

  popout.once("tauri://error", (e) => {
    console.error("Failed to open chart popout:", e);
  });
}

export function isChartOnlyView(): boolean {
  if (typeof window === "undefined") return false;
  const params = new URLSearchParams(window.location.search);
  return params.get("view") === "chart-only";
}
