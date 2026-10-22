'use client'
// Rescue Ready fork: essay question (long written answer, graded by an instructor).
//
// Stored as a CUSTOM task with contents { kind: 'essay', prompt, min_words?,
// max_words?, explanation? } so no new Postgres enum value is needed. The
// server never auto-grades CUSTOM, enforces the word limits at hand-in
// (services/courses/activities/essay.py) and strips `explanation` (the model
// answer / rubric) from students until grading, revealing it only when the
// assignment shows correct answers. Modelled on TaskShortAnswerObject.
import { useAssignments } from '@components/Contexts/Assignments/AssignmentContext'
import { useAssignmentSubmission } from '@components/Contexts/Assignments/AssignmentSubmissionContext'
import {
  useAssignmentsTask,
  useAssignmentsTaskDispatch,
} from '@components/Contexts/Assignments/AssignmentsTaskContext'
import { useLHSession } from '@components/Contexts/LHSessionContext'
import AssignmentBoxUI from '@components/Objects/Activities/Assignment/AssignmentBoxUI'
import {
  getAssignmentTask,
  getAssignmentTaskSubmissionsMe,
  getAssignmentTaskSubmissionsUser,
  handleAssignmentTaskSubmission,
  updateAssignmentTask,
} from '@services/courses/assignments'
import { BookOpenCheck } from 'lucide-react'
import React, { useEffect, useState } from 'react'
import toast from 'react-hot-toast'
import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import { queryKeys } from '@/lib/query/keys'
import { applyManualGrade } from './applyManualGrade'
import { countWords, essayLimits, isEssayContents } from '@/lib/assignments/essay'

type EssayContents = {
  kind: 'essay'
  prompt: string
  min_words: number | null
  max_words: number | null
  explanation: string
}

type TaskEssayObjectProps = {
  view: 'teacher' | 'student' | 'grading'
  assignmentTaskUUID?: string
  user_id?: string
  onGraded?: () => void
}

// Always emits kind: 'essay'. Task updates replace `contents` wholesale, so a
// save that dropped the marker would silently turn the essay into a bare
// CUSTOM task that no view renders.
function normalizeContents(raw: any): EssayContents {
  const { min, max } = essayLimits(raw)
  return {
    kind: 'essay',
    prompt: raw?.prompt ?? '',
    min_words: min,
    max_words: max,
    explanation: raw?.explanation ?? '',
  }
}

const k = (key: string) => `dashboard.assignments.editor.task_editor.essay.${key}`

function TaskEssayObject({ view, assignmentTaskUUID, user_id, onGraded }: TaskEssayObjectProps) {
  const { t } = useTranslation()
  const session = useLHSession() as any
  const access_token = session?.data?.tokens?.access_token
  const assignmentTaskState = useAssignmentsTask() as any
  const assignmentTaskStateHook = useAssignmentsTaskDispatch() as any
  const assignment = useAssignments() as any
  const queryClient = useQueryClient()
  const assignmentSubmission = useAssignmentSubmission() as any
  const submissionStatus =
    Array.isArray(assignmentSubmission) && assignmentSubmission.length > 0
      ? assignmentSubmission[0].submission_status
      : null
  // Answers freeze once handed in (the API 403s any later write and auto-save
  // is off), so the textarea goes read-only rather than accepting lost edits.
  const answerLocked = submissionStatus === 'SUBMITTED' || submissionStatus === 'GRADED'

  const [contents, setContents] = useState<EssayContents>(normalizeContents({}))
  const [studentAnswer, setStudentAnswer] = useState<string>('')
  const [initialAnswer, setInitialAnswer] = useState<string>('')
  // Same hydration race guard as TaskShortAnswerObject: a late load must not
  // overwrite text the learner already started typing.
  const interactedRef = React.useRef(false)
  const [userSubmissions, setUserSubmissions] = useState<any>(null)
  const [userSubmissionObject, setUserSubmissionObject] = useState<any>(null)
  const [taskOutsideProvider, setTaskOutsideProvider] = useState<any>(null)
  // Raw `explanation` presence: the server withholds it from students until
  // grading (and only reveals with show_correct_answers), so render the model
  // answer panel only when it actually arrived.
  const [explanationPresent, setExplanationPresent] = useState(false)

  useEffect(() => {
    if (view === 'teacher' && isEssayContents(assignmentTaskState?.assignmentTask?.contents)) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setContents(normalizeContents(assignmentTaskState.assignmentTask.contents))
    }
  }, [view, assignmentTaskState])

  async function loadTaskDefinition() {
    if (!assignmentTaskUUID) return
    const res = await getAssignmentTask(assignmentTaskUUID, access_token)
    if (res.success) {
      setTaskOutsideProvider(res.data)
      setContents(normalizeContents(res.data.contents))
      setExplanationPresent(
        typeof res.data.contents?.explanation === 'string' && res.data.contents.explanation.trim().length > 0
      )
    }
  }

  async function loadOwnSubmission() {
    if (!assignmentTaskUUID) return
    const res = await getAssignmentTaskSubmissionsMe(
      assignmentTaskUUID,
      assignment.assignment_object.assignment_uuid,
      access_token
    )
    if (res.success && res.data) {
      setUserSubmissions(res.data)
      const saved = typeof res.data.task_submission?.answer === 'string' ? res.data.task_submission.answer : ''
      setInitialAnswer(saved)
      if (!interactedRef.current) setStudentAnswer(saved)
    }
  }

  async function loadUserSubmission() {
    if (!assignmentTaskUUID || !user_id) return
    const res = await getAssignmentTaskSubmissionsUser(
      assignmentTaskUUID,
      user_id,
      assignment.assignment_object.assignment_uuid,
      access_token
    )
    if (res.success && res.data) {
      setUserSubmissions(res.data)
      setUserSubmissionObject(res.data)
      const saved = typeof res.data.task_submission?.answer === 'string' ? res.data.task_submission.answer : ''
      setStudentAnswer(saved)
      setInitialAnswer(saved)
    }
  }

  useEffect(() => {
    /* eslint-disable react-hooks/set-state-in-effect */
    if (view === 'student') {
      loadTaskDefinition()
      loadOwnSubmission()
    } else if (view === 'grading') {
      loadTaskDefinition()
      loadUserSubmission()
    }
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [view, assignmentTaskUUID, assignment, access_token])

  // --- SAVE (teacher) ---
  async function saveFC() {
    if (!assignmentTaskState?.assignmentTask?.assignment_task_uuid) return
    if (contents.min_words && contents.max_words && contents.min_words > contents.max_words) {
      toast.error(t(k('limits_error'), { defaultValue: 'The minimum word count cannot be above the maximum.' }))
      return
    }
    const res = await updateAssignmentTask(
      { contents: normalizeContents(contents) },
      assignmentTaskState.assignmentTask.assignment_task_uuid,
      assignment.assignment_object.assignment_uuid,
      access_token
    )
    if (res.success) {
      assignmentTaskStateHook({ type: 'reload' })
      toast.success(t('dashboard.assignments.editor.toasts.task_updated'))
    } else {
      toast.error(t('dashboard.assignments.editor.toasts.task_update_error'))
    }
  }

  // --- SAVE DRAFT (student) --- same save-without-submit flow as the other
  // task types: persist the text with grade 0; grading happens later.
  async function submitFC(opts?: { silent?: boolean }) {
    if (!assignmentTaskUUID) return true
    const res = await handleAssignmentTaskSubmission(
      {
        assignment_task_submission_uuid: userSubmissions?.assignment_task_submission_uuid || null,
        task_submission: { answer: studentAnswer },
        grade: 0,
        task_submission_grade_feedback: '',
      },
      assignmentTaskUUID,
      assignment.assignment_object.assignment_uuid,
      access_token
    )
    if (res.success) {
      setUserSubmissions(res.data)
      setInitialAnswer(studentAnswer)
      const taskUUID = assignmentTaskUUID
      queryClient.setQueryData(
        queryKeys.assignments.taskSubmission(assignment.assignment_object.assignment_uuid),
        (prev: any) => (prev ? { ...prev, [taskUUID]: res.data } : prev)
      )
      if (!opts?.silent) toast.success(t('dashboard.assignments.editor.toasts.task_saved'))
      return true
    }
    if (!opts?.silent) toast.error(t('dashboard.assignments.editor.toasts.task_save_error'))
    return false
  }

  function onStudentChange(next: string) {
    if (answerLocked) return
    // Client-side max: refuse an edit that grows the essay past the limit
    // (deleting is always allowed). The minimum is shown live and enforced by
    // the server at hand-in, which explains the refusal.
    if (contents.max_words && countWords(next) > contents.max_words && countWords(next) > countWords(studentAnswer)) {
      return
    }
    interactedRef.current = true
    setStudentAnswer(next)
  }

  async function gradeCustomFC(grade: number, feedback?: string) {
    if (!userSubmissions) return
    await applyManualGrade({
      grade,
      feedback,
      maxPoints:
        taskOutsideProvider?.max_grade_value || assignmentTaskState?.assignmentTask?.max_grade_value || 100,
      assignmentTaskUUID,
      assignmentUUID: assignment.assignment_object.assignment_uuid,
      accessToken: access_token,
      username: session?.data?.user?.username,
      assignmentTaskSubmissionUUID: userSubmissions.assignment_task_submission_uuid,
      taskSubmissionPayload: userSubmissions.task_submission,
      onSuccess: () => { loadUserSubmission(); onGraded?.() },
    })
  }

  const words = countWords(studentAnswer)
  const belowMin = !!contents.min_words && words < contents.min_words
  const aboveMax = !!contents.max_words && words > contents.max_words
  const limitText = [
    contents.min_words ? t(k('min_words_hint'), { count: contents.min_words, defaultValue: 'min {{count}}' }) : null,
    contents.max_words ? t(k('max_words_hint'), { count: contents.max_words, defaultValue: 'max {{count}}' }) : null,
  ].filter(Boolean).join(' · ')

  const wordCounter = (
    <p className={`text-xs font-medium ${belowMin || aboveMax ? 'text-rose-600' : 'text-slate-500'}`} aria-live="polite">
      {t(k('word_count'), { count: words, defaultValue: '{{count}} words' })}
      {limitText && ` (${limitText})`}
    </p>
  )

  const modelAnswer = (
    <div className="flex flex-col space-y-1.5 p-3 rounded-md bg-emerald-50 border border-emerald-200">
      <div className="flex items-center space-x-1.5 text-xs font-semibold text-emerald-700">
        <BookOpenCheck size={13} />
        <span>{t(k('model_answer_label'), { defaultValue: 'Model answer / rubric' })}</span>
      </div>
      <p className="text-sm text-emerald-900 whitespace-pre-wrap">{contents.explanation}</p>
    </div>
  )

  const limitInput = (field: 'min_words' | 'max_words', label: string) => (
    <label className="flex flex-col space-y-1 text-xs font-semibold text-slate-500">
      <span>{label}</span>
      <input
        type="number"
        min={1}
        value={contents[field] ?? ''}
        onChange={(e) => {
          const n = parseInt(e.target.value, 10)
          setContents((prev) => ({ ...prev, [field]: Number.isFinite(n) && n > 0 ? n : null }))
        }}
        placeholder={t(k('no_limit'), { defaultValue: 'No limit' })}
        className="w-32 px-3 py-1.5 text-sm font-normal border border-gray-200 rounded-md bg-white"
      />
    </label>
  )

  return (
    <AssignmentBoxUI
      type="essay"
      view={view}
      saveFC={saveFC}
      submitFC={submitFC}
      taskUUID={assignmentTaskUUID}
      gradeCustomFC={gradeCustomFC}
      currentPoints={userSubmissionObject?.grade}
      currentFeedback={userSubmissionObject?.task_submission_grade_feedback}
      maxPoints={taskOutsideProvider?.max_grade_value || assignmentTaskState?.assignmentTask?.max_grade_value}
      dirtyValue={studentAnswer}
      savedValue={initialAnswer}
    >
      <div className="flex flex-col space-y-4">
        {view === 'teacher' && (
          <>
            <label className="flex flex-col space-y-1 text-xs font-semibold text-slate-500">
              <span>{t(k('prompt_label'), { defaultValue: 'Question' })}</span>
              <textarea
                value={contents.prompt}
                onChange={(e) => setContents((prev) => ({ ...prev, prompt: e.target.value }))}
                placeholder={t(k('prompt_placeholder'), { defaultValue: 'What should the student write about?' })}
                rows={3}
                className="px-3 py-2 text-sm font-normal border border-gray-200 rounded-md bg-white resize-y"
              />
            </label>
            <div className="flex flex-wrap gap-4">
              {limitInput('min_words', t(k('min_words_label'), { defaultValue: 'Minimum words' }))}
              {limitInput('max_words', t(k('max_words_label'), { defaultValue: 'Maximum words' }))}
            </div>
            <label className="flex flex-col space-y-1 text-xs font-semibold text-slate-500">
              <span>{t(k('explanation_label'), { defaultValue: 'Model answer or rubric (optional)' })}</span>
              <textarea
                value={contents.explanation}
                onChange={(e) => setContents((prev) => ({ ...prev, explanation: e.target.value }))}
                placeholder={t(k('explanation_placeholder'), { defaultValue: 'What a good answer covers. Only instructors see this.' })}
                rows={3}
                className="px-3 py-2 text-sm font-normal border border-gray-200 rounded-md bg-white resize-y"
              />
              <span className="font-normal text-slate-400">
                {t(k('explanation_help'), {
                  defaultValue: 'Students see it only after grading, and only if the assignment shows correct answers.',
                })}
              </span>
            </label>
          </>
        )}

        {view === 'student' && (
          <>
            {contents.prompt && <p className="text-sm text-slate-700 whitespace-pre-wrap">{contents.prompt}</p>}
            <textarea
              value={studentAnswer}
              onChange={(e) => onStudentChange(e.target.value)}
              readOnly={answerLocked}
              rows={12}
              aria-label={t(k('your_answer_label'), { defaultValue: 'Your answer' })}
              placeholder={t(k('your_answer_placeholder'), { defaultValue: 'Write your answer here.' })}
              className="w-full px-3 py-2 text-sm leading-relaxed border-2 border-gray-200 rounded-md bg-white focus:border-blue-400 focus:ring-2 focus:ring-blue-200 outline-none resize-y"
            />
            {wordCounter}
            {submissionStatus === 'GRADED' && explanationPresent && modelAnswer}
          </>
        )}

        {view === 'grading' && (
          <>
            {contents.prompt && <p className="text-sm text-slate-700 whitespace-pre-wrap">{contents.prompt}</p>}
            <div className="flex flex-col space-y-1">
              <span className="text-xs font-semibold text-slate-500">
                {t(k('student_answer_label'), { defaultValue: "Student's answer" })}
              </span>
              <div className="px-4 py-3 text-sm leading-relaxed text-slate-800 bg-white border border-gray-200 rounded-md whitespace-pre-wrap break-words max-h-[60vh] overflow-y-auto">
                {studentAnswer || (
                  <span className="text-gray-400 italic">{t(k('no_answer'), { defaultValue: 'No answer given' })}</span>
                )}
              </div>
              {wordCounter}
            </div>
            {explanationPresent && modelAnswer}
          </>
        )}
      </div>
    </AssignmentBoxUI>
  )
}

export default TaskEssayObject
