// Rescue Ready fork: what a student whose attempt was graded can do next.
//
// 'retry'           - the "Try again" button works (mirrors the server's
//                     retry eligibility: allow_retries, cap not reached,
//                     max_retries 0 = unlimited, not past due).
// 'out_of_attempts' - failed and no attempt left (or retries off). Since completion
//                     now requires a pass, only an instructor-granted attempt
//                     gets the student unstuck, so the UI says so.
// 'none'            - anything else (passed, past due with attempts left, loading).
export type RetryState = 'retry' | 'out_of_attempts' | 'none'

export function getRetryState(args: {
  allowRetries: boolean
  maxRetries: number
  currentAttempt: number
  // undefined while the grade is still loading; only `false` is a fail.
  passed: boolean | undefined
  pastDue: boolean
}): RetryState {
  const { allowRetries, maxRetries, currentAttempt, passed, pastDue } = args
  const attemptsLeft = maxRetries === 0 || currentAttempt < maxRetries
  if (allowRetries && !pastDue && attemptsLeft) return 'retry'
  if (passed !== false) return 'none'
  // Retries off: the one attempt is used, and an instructor can still grant
  // another (grant_assignment_attempt allows it).
  if (!allowRetries) return 'out_of_attempts'
  // A granted attempt pushes currentAttempt past maxRetries, so >= (not ===).
  if (maxRetries > 0 && currentAttempt >= maxRetries) return 'out_of_attempts'
  return 'none'
}
