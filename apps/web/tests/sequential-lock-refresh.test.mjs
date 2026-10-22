// Rescue Ready fork: after progress that may open a sequential-locked chapter,
// the cached course tree and cached (prefetched) locked activity reads must be
// dropped, or the student keeps seeing the lock after finishing the chapter.
import { describe, expect, test } from "bun:test";
import { QueryClient } from "@tanstack/react-query";

import { refreshAfterSequentialProgress } from "../components/Pages/Courses/sequentialLock.ts";

const lockedCourse = {
  course_uuid: "course_abc",
  chapters: [
    { id: 1, is_locked: false, activities: [] },
    { id: 2, is_locked: true, lock_reason: "sequential", activities: [] },
  ],
};

function seeded() {
  const qc = new QueryClient();
  qc.setQueryData(["course", "abc", "meta"], lockedCourse);
  qc.setQueryData(["activity", "activity_next"], { is_locked: true });
  return qc;
}

describe("refreshAfterSequentialProgress", () => {
  test("drops locked activity reads and marks the course tree stale", async () => {
    const qc = seeded();
    await refreshAfterSequentialProgress(qc, lockedCourse);
    expect(qc.getQueryData(["activity", "activity_next"])).toBeUndefined();
    expect(qc.getQueryState(["course", "abc", "meta"]).isInvalidated).toBe(true);
  });

  test("leaves the cache alone when nothing is sequential-locked", async () => {
    const qc = seeded();
    const open = { ...lockedCourse, chapters: [{ id: 1, is_locked: true, lock_reason: null }] };
    await refreshAfterSequentialProgress(qc, open);
    expect(qc.getQueryData(["activity", "activity_next"])).toEqual({ is_locked: true });
    expect(qc.getQueryState(["course", "abc", "meta"]).isInvalidated).toBe(false);
  });
});
