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

// Hand-in size ceiling. Must equal ESSAY_SNAPSHOT_MAX_CHARS in essay.py: the
// server refuses a larger answer because the audit snapshot would not keep it.
export const ESSAY_MAX_JSON_CHARS = 100_000

// Length of Python's json.dumps({"answer": text}) (default ensure_ascii), which
// is how the server measures the ceiling. Escaping makes it longer than
// text.length: quotes, backslashes and \n\r\t\b\f take 2 chars; other control
// chars, DEL and every non-ASCII UTF-16 unit take 6 (\uXXXX), so an emoji's
// surrogate pair takes 12.
export function essayJsonLength(text: string): number {
  let n = '{"answer": ""}'.length
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i)
    if (c === 0x22 || c === 0x5c || c === 0x0a || c === 0x0d || c === 0x09 || c === 0x08 || c === 0x0c) n += 2
    else if (c < 0x20 || c > 0x7e) n += 6
    else n += 1
  }
  return n
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
