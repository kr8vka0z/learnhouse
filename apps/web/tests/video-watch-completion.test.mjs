// Rescue Ready fork: when a video counts as watched (auto-completes the activity).
import { describe, expect, test } from "bun:test";

import { createPlayClock, isVideoWatched, playedSeconds } from "../lib/video/watchCompletion.ts";

const ranges = (...pairs) => ({
  length: pairs.length,
  start: (i) => pairs[i][0],
  end: (i) => pairs[i][1],
});

describe("isVideoWatched", () => {
  test("watched through to the end counts", () => {
    expect(isVideoWatched({ currentTime: 100, duration: 100, played: 100, ended: true })).toBe(true);
    expect(isVideoWatched({ currentTime: 96, duration: 100, played: 96 })).toBe(true);
  });

  test("seeking to the end without watching does not count", () => {
    const played = playedSeconds(ranges([0, 5], [95, 100]));
    expect(isVideoWatched({ currentTime: 100, duration: 100, played, ended: true })).toBe(false);
  });

  test("watching most of it but stopping early does not count", () => {
    expect(isVideoWatched({ currentTime: 90, duration: 100, played: 90 })).toBe(false);
  });

  test("honours start/end trims", () => {
    expect(isVideoWatched({ currentTime: 59, duration: 100, played: 39, startTime: 20, endTime: 60 })).toBe(true);
    expect(isVideoWatched({ currentTime: 59, duration: 100, played: 10, startTime: 20, endTime: 60 })).toBe(false);
  });

  test("unknown duration never counts", () => {
    expect(isVideoWatched({ currentTime: 0, duration: NaN, played: 0, ended: true })).toBe(false);
  });
});

describe("createPlayClock", () => {
  test("counts only time spent playing, scaled by rate", () => {
    let t = 0;
    const clock = createPlayClock(() => t);
    clock.play(1); t = 10_000; clock.pause();
    t = 50_000; // paused time is ignored
    clock.play(2); t = 55_000;
    expect(clock.seconds()).toBe(20);
  });
});
