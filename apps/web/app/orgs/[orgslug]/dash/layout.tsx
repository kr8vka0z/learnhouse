import { Metadata } from 'next'
import React from 'react'
import ClientAdminLayout from './ClientAdminLayout'

export const metadata: Metadata = {
  // Rescue Ready fork: white-label; staff see no platform name in the tab.
  title: 'Dashboard',
}

async function DashboardLayout(
  props: {
    children: React.ReactNode
    params: Promise<any>
  }
) {
  const params = await props.params;

  const {
    children
  } = props;

  return (
    <>
      <ClientAdminLayout
        params={params}>
        {children}
      </ClientAdminLayout>
    </>
  )
}

export default DashboardLayout
