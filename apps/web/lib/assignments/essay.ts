// Rescue Ready fork: essay task helpers shared by the editor, player and grader.
// An essay is a CUSTOM task whose contents carry kind: 'essay'. These rules
// mirror apps/api/src/services/courses/activities/essay.py; if they drift, a
// student can pass the browser check and still be refused at hand-in.

export function isEssayContents(contents: unknown): boolean {
  return !!contents && typeof contents === 'object' && (contents as any).kind === 'essay'
}

export function isEssayTask(task: any): boolean {
  return task?.assignment_type === 'CUSTOM' && isEssayContents(task?.contents)
}

// Python's str.split(): any run of whitespace separates words.
export function countWords(text: string): number {
  return text.trim().split(/\s+/).filter(Boolean).length
}

function limit(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value > 0 ? value : null
}

// Malformed or contradictory limits (min above max) count as no limits, as on
// the server.
export function essayLimits(contents: any): { min: number | null; max: number | null } {
  const min = limit(contents?.min_words)
  const max = limit(contents?.max_words)
  if (min !== null && max !== null && min > max) return { min: null, max: null }
  return { min, max }
}
