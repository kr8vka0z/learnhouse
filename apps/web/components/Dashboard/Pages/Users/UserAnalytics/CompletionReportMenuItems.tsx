// Rescue Ready fork: "Completion report" entries for the dossier Export menu.
// The per-course completion report is the school's official module-completion
// record; one CSV + printable entry per course the student is enrolled in.
'use client'
import React, { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { getAPIUrl } from '@services/config/config'
import { useLHSession } from '@components/Contexts/LHSessionContext'
import { useOrg } from '@components/Contexts/OrgContext'

interface Course {
  course_uuid?: string | null
  course_name?: string | null
}

interface Props {
  userId: number
  courses: Course[]
  onDone: () => void
}

export function completionReportUrl(orgId: number, userId: number, courseUuid: string, format: 'csv' | 'html') {
  const params = new URLSearchParams({ org_id: String(orgId), course_uuid: courseUuid, format })
  return `${getAPIUrl()}audit/completion-report/user/${userId}?${params}`
}

export async function downloadWithToken(url: string, token: string, filename: string) {
  const resp = await fetch(url, { headers: { Authorization: `Bearer ${token}` } })
  if (!resp.ok) throw new Error(`${resp.status}`)
  const blobUrl = URL.createObjectURL(await resp.blob())
  const a = document.createElement('a')
  a.href = blobUrl
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(blobUrl)
}

export default function CompletionReportMenuItems({ userId, courses, onDone }: Props) {
  const { t } = useTranslation()
  const session = useLHSession() as any
  const org = useOrg() as any
  const token = session?.data?.tokens?.access_token
  const [busy, setBusy] = useState(false)
  const list = courses.filter((c) => !!c.course_uuid)
  if (!org?.id || !token || list.length === 0) return null

  async function csv(c: Course) {
    if (busy) return
    setBusy(true)
    try {
      await downloadWithToken(
        completionReportUrl(org.id, userId, c.course_uuid!, 'csv'),
        token,
        `completion-${userId}-${c.course_uuid}.csv`
      )
      onDone()
    } catch (err) {
      console.error('Completion report failed:', err)
    } finally {
      setBusy(false)
    }
  }

  async function print(c: Course) {
    // Open the tab synchronously (inside the click) so pop-up blockers allow it,
    // then point it at the authenticated HTML once fetched.
    const win = window.open('', '_blank')
    try {
      const resp = await fetch(completionReportUrl(org.id, userId, c.course_uuid!, 'html'), {
        headers: { Authorization: `Bearer ${token}` },
      })
      if (!resp.ok) throw new Error(`${resp.status}`)
      const blob = new Blob([await resp.text()], { type: 'text/html' })
      if (win) win.location.href = URL.createObjectURL(blob)
      onDone()
    } catch (err) {
      win?.close()
      console.error('Completion report failed:', err)
    }
  }

  const label = t('dashboard.users.analytics.completion_report_csv', { defaultValue: 'Completion report (CSV)' })
  return (
    <div className="border-t border-gray-100">
      {list.map((c) => (
        <div key={c.course_uuid!} className="flex items-center">
          <button
            onClick={() => csv(c)}
            disabled={busy}
            className="flex-1 min-w-0 text-start px-3 py-2 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50"
          >
            <span className="block">{label}</span>
            {list.length > 1 && <span className="block truncate text-[11px] text-gray-500">{c.course_name}</span>}
          </button>
          <button
            onClick={() => print(c)}
            className="px-3 py-2 text-[11px] font-medium text-gray-500 hover:bg-gray-50"
          >
            {t('dashboard.users.analytics.completion_report_print', { defaultValue: 'Print' })}
          </button>
        </div>
      ))}
    </div>
  )
}
