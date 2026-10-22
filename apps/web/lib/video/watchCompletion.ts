// Rescue Ready fork: decides when a video activity counts as watched to the end,
// so the player can mark it complete the same way the learner's checkbox does.

// Reaching the last 5% (or the `ended` event) is "the end"; having actually
// played 90% of the span is what stops a seek-to-the-end from counting.
export const END_FRACTION = 0.95
export const WATCHED_FRACTION = 0.9

type Ranges = { length: number; start(i: number): number; end(i: number): number }

/** Total seconds covered by a media element's `played` ranges. */
export function playedSeconds(ranges: Ranges | null | undefined): number {
  if (!ranges) return 0
  let total = 0
  for (let i = 0; i < ranges.length; i++) total += Math.max(0, ranges.end(i) - ranges.start(i))
  return total
}

/** The part of the video the activity asks for (start/end trims honoured). */
export function watchSpan(duration: number, startTime?: number | null, endTime?: number | null): number {
  if (!Number.isFinite(duration) || duration <= 0) return 0
  const start = Math.max(0, startTime || 0)
  const end = endTime && endTime > start && endTime < duration ? endTime : duration
  return Math.max(0, end - start)
}

export function isVideoWatched(opts: {
  currentTime: number
  duration: number
  played: number
  ended?: boolean
  startTime?: number | null
  endTime?: number | null
}): boolean {
  const span = watchSpan(opts.duration, opts.startTime, opts.endTime)
  if (span <= 0) return false
  const start = Math.max(0, opts.startTime || 0)
  const atEnd = !!opts.ended || opts.currentTime >= start + span * END_FRACTION
  return atEnd && opts.played >= span * WATCHED_FRACTION
}

/**
 * Seconds of video time spent playing, for players (YouTube iframe) that expose
 * play/pause events but no `played` ranges. Wall time is scaled by the playback
 * rate at the moment play started.
 * ponytail: a rate change mid-segment is counted at the old rate until the next
 * pause; good enough to tell "watched" from "skipped to the end".
 */
export function createPlayClock(now: () => number = () => Date.now()) {
  let total = 0
  let since: number | null = null
  let rate = 1
  return {
    play(playbackRate = 1) {
      if (since === null) {
        since = now()
        rate = playbackRate > 0 ? playbackRate : 1
      }
    },
    pause() {
      if (since !== null) {
        total += ((now() - since) / 1000) * rate
        since = null
      }
    },
    seconds() {
      return total + (since !== null ? ((now() - since) / 1000) * rate : 0)
    },
  }
}
