// Rescue Ready fork: essay helpers must agree with the server's hand-in check
// (apps/api/src/services/courses/activities/essay.py).
import { describe, expect, test } from "bun:test";

import { countWords, essayLimits, isEssayTask } from "../lib/assignments/essay.ts";

describe("countWords", () => {
  test("splits on any run of whitespace like Python str.split()", () => {
    expect(countWords("  one\ntwo   three\tfour ")).toBe(4);
    expect(countWords("")).toBe(0);
    expect(countWords("   \n ")).toBe(0);
  });
});

describe("essayLimits", () => {
  test("keeps positive integer limits", () => {
    expect(essayLimits({ min_words: 3, max_words: 5 })).toEqual({ min: 3, max: 5 });
  });

  test("treats malformed or contradictory limits as none", () => {
    expect(essayLimits({ min_words: "3", max_words: 0 })).toEqual({ min: null, max: null });
    expect(essayLimits({ min_words: 9, max_words: 5 })).toEqual({ min: null, max: null });
  });
});

describe("isEssayTask", () => {
  test("only CUSTOM tasks marked kind essay are essays", () => {
    expect(isEssayTask({ assignment_type: "CUSTOM", contents: { kind: "essay" } })).toBe(true);
    expect(isEssayTask({ assignment_type: "CUSTOM", contents: {} })).toBe(false);
    expect(isEssayTask({ assignment_type: "SHORT_ANSWER", contents: { kind: "essay" } })).toBe(false);
  });
});
