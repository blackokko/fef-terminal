// IMPROVEMENTS #17: audible chime on HIGH-confidence fires.
// Pure Web Audio API — no asset file needed.

let audioCtx: AudioContext | null = null;

function ctx(): AudioContext | null {
  if (typeof window === "undefined") return null;
  if (audioCtx) return audioCtx;
  try {
    const AC = window.AudioContext || (window as any).webkitAudioContext;
    audioCtx = new AC();
    return audioCtx;
  } catch {
    return null;
  }
}

/** Two-tone bullish chime (rising). Played on HIGH LONG_CALLS. */
export function playBullishChime(): void {
  playTones([523.25, 783.99], 0.18);  // C5, G5
}

/** Two-tone bearish chime (falling). Played on HIGH LONG_PUTS. */
export function playBearishChime(): void {
  playTones([783.99, 523.25], 0.18);  // G5, C5
}

function playTones(frequencies: number[], duration: number, volume = 0.15): void {
  const c = ctx();
  if (!c) return;
  if (c.state === "suspended") {
    void c.resume();
  }
  const now = c.currentTime;
  frequencies.forEach((freq, i) => {
    const osc = c.createOscillator();
    const gain = c.createGain();
    osc.type = "sine";
    osc.frequency.value = freq;
    const start = now + i * duration;
    const stop = start + duration;
    gain.gain.setValueAtTime(0, start);
    gain.gain.linearRampToValueAtTime(volume, start + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.001, stop);
    osc.connect(gain);
    gain.connect(c.destination);
    osc.start(start);
    osc.stop(stop + 0.05);
  });
}

/** Flash the document title for `flashes` cycles. Visible even when window unfocused. */
export function flashTitle(message: string, flashes = 6, intervalMs = 600): void {
  if (typeof document === "undefined") return;
  const original = document.title;
  let count = 0;
  const id = window.setInterval(() => {
    document.title = count % 2 === 0 ? message : original;
    count += 1;
    if (count > flashes * 2) {
      window.clearInterval(id);
      document.title = original;
    }
  }, intervalMs);
}
