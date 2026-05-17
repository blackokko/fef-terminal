// Compact, scan-friendly money formatters.
//
// formatExposureM(32703.22) -> "+32.7B"
// formatExposureM(-38094.13) -> "-38.1B"
// formatExposureM(1336333.38) -> "+1.34T"
// formatExposureM(-2.65) -> "-2.7M"
// formatExposureM(0.42) -> "+420k"

export function formatExposureM(valueM: number | null | undefined): string {
  if (valueM == null || !isFinite(valueM)) return "—";
  const sign = valueM > 0 ? "+" : valueM < 0 ? "-" : "";
  const abs = Math.abs(valueM);
  if (abs >= 1_000_000) return `${sign}${(abs / 1_000_000).toFixed(2)}T`;
  if (abs >= 1_000) return `${sign}${(abs / 1_000).toFixed(1)}B`;
  if (abs >= 1) return `${sign}${abs.toFixed(1)}M`;
  if (abs >= 0.001) return `${sign}${(abs * 1000).toFixed(0)}k`;
  return `${sign}0`;
}

// Wall-label variant — no leading sign for zero, includes $ prefix for charts
export function formatExposureLabel(valueM: number): string {
  return `$${formatExposureM(valueM)}`;
}

// Cumulative drift formatter (drift values come in $M already)
export function formatDrift(valueM: number | null | undefined): string {
  return formatExposureM(valueM);
}

// Dollar amount formatter (P/L, position cost, etc.) — input is raw dollars
export function formatDollars(value: number | null | undefined, opts: { sign?: boolean } = {}): string {
  if (value == null || !isFinite(value)) return "—";
  const sign = opts.sign && value > 0 ? "+" : value < 0 ? "-" : "";
  const abs = Math.abs(value);
  if (abs >= 1_000_000) return `${sign}$${(abs / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `${sign}$${(abs / 1_000).toFixed(1)}k`;
  return `${sign}$${abs.toFixed(2)}`;
}
