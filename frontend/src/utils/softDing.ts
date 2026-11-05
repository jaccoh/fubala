/**
 * Soft "ding" sound using Web Audio API.
 * Reuses a single AudioContext to avoid browser warnings.
 */

let audioContext: AudioContext | null = null;

/**
 * Play a short, soft ding sound (600Hz sine wave with exponential decay).
 * Safe to call multiple times; creates a new oscillator each time.
 */
export function playReviewDing(): void {
  if (!audioContext) {
    audioContext = new AudioContext();
  }

  // Resume if suspended (e.g., before user interaction)
  if (audioContext.state === "suspended") {
    audioContext.resume().catch(() => {
      // Audio may remain suspended if no user interaction yet; silently ignore
    });
  }

  const osc = audioContext.createOscillator();
  const gain = audioContext.createGain();

  osc.type = "sine";
  osc.frequency.value = 600; // Hz

  // Exponential decay from 0.3 to 0.01 over 0.25 seconds
  const now = audioContext.currentTime;
  gain.gain.setValueAtTime(0.3, now);
  gain.gain.exponentialRampToValueAtTime(0.01, now + 0.25);

  osc.connect(gain);
  gain.connect(audioContext.destination);

  osc.start(now);
  osc.stop(now + 0.25);
}
