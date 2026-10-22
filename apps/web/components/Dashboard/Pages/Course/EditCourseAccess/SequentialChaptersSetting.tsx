// Rescue Ready fork: on/off toggle for "Sequential chapters" (pass-to-unlock).
// Stored in course.extra_metadata.rr_sequential_chapters; the API enforces it.
// extra_metadata is replaced wholesale on save, so the toggle writes back the
// whole object with only its own key changed.
'use client'
import { useCourseFieldSync } from '@components/Contexts/CourseContext'
import { SEQUENTIAL_SETTING_KEY } from '@components/Pages/Courses/sequentialLock'
import { ListOrdered } from 'lucide-react'
import React, { useCallback } from 'react'
import { useTranslation } from 'react-i18next'

export default function SequentialChaptersSetting() {
  const { t } = useTranslation()
  const { syncChanges, courseStructure, isSaving } = useCourseFieldSync('editCourseSequential')
  const enabled = courseStructure?.extra_metadata?.[SEQUENTIAL_SETTING_KEY] === true

  const onChange = useCallback(
    (value: boolean) => {
      syncChanges(
        {
          extra_metadata: {
            ...(courseStructure?.extra_metadata ?? {}),
            [SEQUENTIAL_SETTING_KEY]: value,
          },
        },
        true,
      )
    },
    [courseStructure?.extra_metadata, syncChanges],
  )

  if (!courseStructure) return null

  return (
    <div className="px-6 py-5 border-b border-gray-100">
      <label className={`flex items-start gap-3 cursor-pointer ${isSaving ? 'opacity-50 pointer-events-none' : ''}`}>
        <ListOrdered size={18} className="text-gray-500 mt-0.5 shrink-0" />
        <span className="flex-1">
          <span className="block text-sm font-semibold text-gray-800">
            {t('dashboard.courses.access.sequential.title', 'Sequential chapters')}
          </span>
          <span className="block text-sm text-gray-500 mt-0.5">
            {t(
              'dashboard.courses.access.sequential.description',
              'Students must finish every activity in a chapter before the next chapter opens. Assignments graded by an instructor count once submitted.',
            )}
          </span>
        </span>
        <input
          type="checkbox"
          role="switch"
          aria-checked={enabled}
          checked={enabled}
          onChange={(e) => onChange(e.target.checked)}
          className="mt-1 h-5 w-5 accent-gray-900 shrink-0"
        />
      </label>
    </div>
  )
}
