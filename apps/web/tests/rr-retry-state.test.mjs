// Rescue Ready fork: out-of-attempts detection for the graded assignment view.
import { describe, expect, test } from "bun:test";

import { getRetryState } from "../lib/assignments/retryState.ts";

const base = { allowRetries: true, maxRetries: 2, currentAttempt: 1, passed: false, pastDue: false };

describe("getRetryState", () => {
  test("failed with attempts left can retry", () => {
    expect(getRetryState(base)).toBe("retry");
  });

  test("failed on the last allowed attempt is out of attempts", () => {
    expect(getRetryState({ ...base, currentAttempt: 2 })).toBe("out_of_attempts");
  });

  test("failing an instructor-granted extra attempt is out of attempts again", () => {
    expect(getRetryState({ ...base, currentAttempt: 3 })).toBe("out_of_attempts");
  });

  test("passing on the last attempt is not out of attempts", () => {
    expect(getRetryState({ ...base, currentAttempt: 2, passed: true })).toBe("none");
  });

  test("grade still loading is not treated as a fail", () => {
    expect(getRetryState({ ...base, currentAttempt: 2, passed: undefined })).toBe("none");
  });

  test("unlimited retries never run out", () => {
    expect(getRetryState({ ...base, maxRetries: 0, currentAttempt: 9 })).toBe("retry");
  });

  test("past due with attempts left is not out of attempts and cannot retry", () => {
    expect(getRetryState({ ...base, pastDue: true })).toBe("none");
  });

  test("retries off and failed is out of attempts (an instructor can still grant one)", () => {
    expect(getRetryState({ ...base, allowRetries: false, maxRetries: 0 })).toBe("out_of_attempts");
  });

  test("retries off and passed shows nothing", () => {
    expect(getRetryState({ ...base, allowRetries: false, maxRetries: 0, passed: true })).toBe("none");
  });
});
