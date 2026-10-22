// Rescue Ready fork: course-level "Download completion records (CSV)" for org
// admins: every enrolled student's per-activity completion, for the school's records.
'use client'
import React, { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { FileSpreadsheet, Loader2 } from 'lucide-react'
import toast from 'react-hot-toast'
import { getAPIUrl } from '@services/config/config'
import { useLHSession } from '@components/Contexts/LHSessionContext'
import { useOrg } from '@components/Contexts/OrgContext'
import useAdminStatus from '@components/Hooks/useAdminStatus'
import { downloadWithToken } from '@components/Dashboard/Pages/Users/UserAnalytics/CompletionReportMenuItems'

export default function CourseCompletionRecordsButton({ courseUuid }: { courseUuid: string }) {
  const { t } = useTranslation()
  const session = useLHSession() as any
  const org = useOrg() as any
  // Same rule the API enforces (organizations.update), so instructors without
  // it never see a button that would only return 403.
  const { canManageOrg } = useAdminStatus()
  const [busy, setBusy] = useState(false)
  const token = session?.data?.tokens?.access_token
  if (!canManageOrg || !org?.id || !token || !courseUuid) return null

  const label = t('dashboard.courses.completion_records_csv', { defaultValue: 'Download completion records (CSV)' })

  async function download() {
    if (busy) return
    setBusy(true)
    try {
      const params = new URLSearchParams({ org_id: String(org.id) })
      await downloadWithToken(
        `${getAPIUrl()}audit/completion-report/course/${encodeURIComponent(courseUuid)}?${params}`,
        token,
        `completion-records-${courseUuid}.csv`
      )
    } catch (err) {
      console.error('Completion records download failed:', err)
      toast.error(t('dashboard.courses.completion_records_error', { defaultValue: 'Could not download completion records' }))
    } finally {
      setBusy(false)
    }
  }

  // Carries its own leading divider so the toolbar has no double rule when hidden.
  return (
    <>
    <div className="w-px self-stretch bg-neutral-200/80" />
    <button
      onClick={download}
      disabled={busy}
      aria-label={label}
      title={label}
      className="px-2.5 sm:px-3.5 py-2 text-sm font-semibold text-neutral-600 bg-neutral-50/70 hover:bg-neutral-100/70 transition-colors flex items-center space-x-1.5 disabled:opacity-50"
    >
      {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <FileSpreadsheet className="w-4 h-4" />}
      <span className="hidden sm:inline">{t('dashboard.courses.completion_records_short', { defaultValue: 'Records' })}</span>
    </button>
    </>
  )
}
