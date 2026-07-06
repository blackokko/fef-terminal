import { useEffect, useRef, useState, type CSSProperties } from "react";

/**
 * A number input for settings that you can actually type into.
 *
 * The old pattern committed to the API on every `onChange` keystroke, which
 * flipped a `busy` flag → `disabled` → the input lost focus after one char.
 * This keeps a LOCAL draft while you type and only commits on blur / Enter,
 * and it never disables itself mid-type. A background refresh/poll only
 * syncs into the box when you're NOT focused, so it can't clobber your typing.
 */
export function NumberSetting({
  value,
  onCommit,
  step,
  min,
  max,
  className = "settings-input",
  disabled = false,
  style,
  title,
}: {
  value: number;
  onCommit: (n: number) => void;
  step?: string | number;
  min?: number;
  max?: number;
  className?: string;
  /** Optional: disable for non-typing reasons (e.g. not connected). NOT tied
   *  to the per-keystroke save flag — that was the focus-stealing bug. */
  disabled?: boolean;
  style?: CSSProperties;
  title?: string;
}) {
  const [draft, setDraft] = useState(String(value));
  const focused = useRef(false);

  // Only adopt external value changes when the user isn't editing.
  useEffect(() => {
    if (!focused.current) setDraft(String(value));
  }, [value]);

  const commit = () => {
    const n = Number(draft);
    if (draft.trim() !== "" && !Number.isNaN(n)) {
      const clamped = Math.min(max ?? Infinity, Math.max(min ?? -Infinity, n));
      if (clamped !== value) onCommit(clamped);
      if (clamped !== n) setDraft(String(clamped));
    } else {
      setDraft(String(value)); // revert empty / invalid back to the live value
    }
  };

  return (
    <input
      type="number"
      step={step}
      min={min}
      max={max}
      value={draft}
      disabled={disabled}
      className={className}
      style={style}
      title={title}
      onFocus={() => {
        focused.current = true;
      }}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => {
        focused.current = false;
        commit();
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
      }}
    />
  );
}
