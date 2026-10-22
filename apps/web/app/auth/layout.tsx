import type { ReactNode } from 'react'
import { OrgProvider } from '@components/Contexts/OrgContext'
import OrgLanguageSync from '@components/Contexts/OrgLanguageSync'
import { getAuthOrgSlug } from '@services/org/orgResolution'
// Rescue Ready fork: org favicon on the auth pages too (see generateMetadata).
import type { Metadata } from 'next'
import { getOrganizationContextInfo } from '@services/organizations/orgs'
import { getOrgFaviconMediaDirectory } from '@services/media/media'

// Rescue Ready fork: the auth pages sit outside app/orgs/[orgslug], whose
// layout sets the org favicon, so sign-in showed the default LearnHouse icon.
// Same lookup as that layout.
export async function generateMetadata(): Promise<Metadata> {
    const orgslug = await getAuthOrgSlug()
    if (!orgslug) return {}
    try {
        const org = await getOrganizationContextInfo(orgslug, {
            revalidate: 86400,
            tags: ['organizations'],
        })
        const faviconImage = org?.config?.config?.customization?.general?.favicon_image || org?.config?.config?.general?.favicon_image
        if (faviconImage) {
            return { icons: { icon: getOrgFaviconMediaDirectory(org.org_uuid, faviconImage) } }
        }
    } catch {
        // A favicon lookup failure must not break the page's metadata.
    }
    return {}
}

export default async function AuthLayout({
    children,
}: {
    children: ReactNode
}) {
    const orgslug = await getAuthOrgSlug()

    // No org slug → bare apex (learn.io) → generic, org-less auth pages. No
    // OrgProvider; the page renders generic LearnHouse branding.
    if (!orgslug) {
        return <>{children}</>
    }

    return (
        <OrgProvider orgslug={orgslug}>
            <OrgLanguageSync />
            {children}
        </OrgProvider>
    )
}
