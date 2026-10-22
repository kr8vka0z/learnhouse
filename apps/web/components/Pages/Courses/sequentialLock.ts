// Rescue Ready fork: helpers for the pass-to-unlock ("Sequential chapters")
// course setting. The API marks a chapter/activity with
// lock_reason === 'sequential' and lock_requires_chapter = { chapter_uuid,
// chapter_name, next_activity_uuid }; every place that shows a lock reads it
// through here so the course page, activity page and progress bar agree.

import type { QueryClient } from '@tanstack/react-query'
import { queryKeys } from '@/lib/query/keys'

export const SEQUENTIAL_SETTING_KEY = 'rr_sequential_chapters'

export function isSequentialLocked(item: any): boolean {
  return !!item?.is_locked && item?.lock_reason === 'sequential'
}

/** "Finish <chapter> to unlock", or null when the lock is not sequential. */
export function sequentialLockHint(item: any, t: any): string | null {
  if (!isSequentialLocked(item)) return null
  const chapter = item.lock_requires_chapter?.chapter_name ?? ''
  return t('course.sequential_locked_hint', {
    defaultValue: 'Finish {{chapter}} to unlock',
    chapter,
  })
}

/** Path (after the org prefix) to where the student should pick up. */
export function sequentialUnlockPath(item: any, courseid: string): string | null {
  if (!isSequentialLocked(item)) return null
  const next = item.lock_requires_chapter?.next_activity_uuid
  if (!next) return `/course/${courseid}`
  return `/course/${courseid}/activity/${String(next).replace('activity_', '')}`
}

export function isSequentialCourse(course: any): boolean {
  return course?.extra_metadata?.[SEQUENTIAL_SETTING_KEY] === true
}

/** True when the course tree this student holds still has sequential locks. */
export function courseHasSequentialLocks(course: any): boolean {
  return (course?.chapters ?? []).some((chapter: any) => isSequentialLocked(chapter))
}

/**
 * After a completion or submission that may open the next chapter, drop the
 * cached course tree and any cached (often prefetched) activity reads: they
 * were fetched while locked and would otherwise show the lock until their
 * staleTime runs out. Only runs while something is sequential-locked.
 */
export async function refreshAfterSequentialProgress(queryClient: QueryClient, course: any): Promise<void> {
  if (!courseHasSequentialLocks(course) || !course?.course_uuid) return
  const clean = String(course.course_uuid).replace('course_', '')
  queryClient.removeQueries({ queryKey: ['activity'], type: 'inactive' })
  await queryClient.invalidateQueries({ queryKey: queryKeys.courses.meta(clean) })
}
