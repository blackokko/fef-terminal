/**
 * Display helpers — convert backend enum/field strings (UPPER_SNAKE_CASE)
 * into human-readable labels for the UI. Use these everywhere a backend
 * code is shown to the user.
 */

export function displayDirection(d: string | null | undefined): string {
  if (!d) return "—";
  const map: Record<string, string> = {
    LONG_CALLS: "Long calls",
    LONG_PUTS: "Long puts",
    WAIT: "Wait",
  };
  return map[d] ?? humanize(d);
}

export function displayConfidence(c: string | null | undefined): string {
  if (!c) return "—";
  const map: Record<string, string> = {
    HIGH: "High",
    MEDIUM: "Medium",
    LOW: "Low",
    NONE: "—",
  };
  return map[c] ?? humanize(c);
}

export function displayStatus(s: string | null | undefined): string {
  if (!s) return "—";
  const map: Record<string, string> = {
    SUBMITTED: "Submitted",
    FILLED: "Filled",
    PARTIAL: "Partial",
    CLOSED: "Closed",
    STOPPED: "Stopped",
    CANCELLED: "Cancelled",
    REJECTED: "Rejected",
    PENDING: "Pending",
    OPEN: "Open",
  };
  return map[s] ?? humanize(s);
}

export function displayRegime(r: string | null | undefined): string {
  if (!r) return "—";
  const map: Record<string, string> = {
    PIN_DAY: "Pin day",
    TREND_DAY: "Trend day",
    CHOP: "Chop",
    ABSORPTION_HEAVY: "Absorption heavy",
    WALL_TEST: "Wall test",
    OPEN_DRIVE: "Open drive",
    POWER_HOUR: "Power hour",
    HIGH_VOL: "High vol",
    NORMAL: "Normal",
  };
  return map[r] ?? humanize(r);
}

/**
 * Format a Date / epoch as 12-hour Central Time with am/pm.
 *
 * Examples:
 *   formatTimeCT(d)              → "1:30pm CT"
 *   formatTimeCT(d, {seconds:1}) → "1:30:42pm CT"
 *   formatTimeCT(d, {ct:false})  → "1:30pm"
 *
 * Used everywhere in the UI so time displays are consistent (12-hour, CT,
 * never UTC or military). The user explicitly requested this.
 */
export function formatTimeCT(
  d: Date | number | string | null | undefined,
  opts: { seconds?: boolean; ct?: boolean } = {},
): string {
  if (d == null) return "—";
  const date = typeof d === "string" ? new Date(d) : (d instanceof Date ? d : new Date(d));
  if (isNaN(date.getTime())) return "—";
  const seconds = opts.seconds ?? false;
  const includeCT = opts.ct ?? true;
  // Build parts manually so we get the lowercase "am/pm" format the user
  // asked for (Intl returns "AM"/"PM"). Use formatToParts on the Chicago tz.
  const fmt = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Chicago",
    hour: "numeric",
    minute: "2-digit",
    second: seconds ? "2-digit" : undefined,
    hour12: true,
  });
  const parts = fmt.formatToParts(date);
  let hour = "", minute = "", sec = "", period = "";
  for (const p of parts) {
    if (p.type === "hour") hour = p.value;
    else if (p.type === "minute") minute = p.value;
    else if (p.type === "second") sec = p.value;
    else if (p.type === "dayPeriod") period = p.value.toLowerCase();
  }
  const time = seconds
    ? `${hour}:${minute}:${sec}${period}`
    : `${hour}:${minute}${period}`;
  return includeCT ? `${time} CT` : time;
}

/**
 * Format a Date as a full date+time in CT.
 *
 * Example: "May 19, 1:30pm CT"
 */
export function formatDateTimeCT(
  d: Date | number | string | null | undefined,
  opts: { seconds?: boolean } = {},
): string {
  if (d == null) return "—";
  const date = typeof d === "string" ? new Date(d) : (d instanceof Date ? d : new Date(d));
  if (isNaN(date.getTime())) return "—";
  const dateFmt = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Chicago",
    month: "short",
    day: "numeric",
  });
  const datePart = dateFmt.format(date);
  const timePart = formatTimeCT(date, { seconds: opts.seconds, ct: false });
  return `${datePart}, ${timePart} CT`;
}

/** Generic fallback — UPPER_SNAKE → Title Case */
export function humanize(s: string): string {
  if (!s) return s;
  return s
    .toLowerCase()
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}
