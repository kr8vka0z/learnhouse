// Rescue Ready fork: white-label switches and the AGPL source-code link.
//
// The fork's deployment presents itself as the school's own site, so
// platform links (docs, help, website, upsells) and LearnHouse marks that org
// settings can't hide are gated on RR_WHITE_LABEL at their render sites. The
// org's name, logo, favicon and color still come from org settings.

export const RR_WHITE_LABEL = true

// Sign-in panel background in white-label (upstream's is a black gradient set
// inline, so CSS can't restyle it): primary navy to deep navy. White text on
// either end is well above WCAG AA.
export const RR_AUTH_PANEL_GRADIENT = 'linear-gradient(041.61deg, #0B1F5C 7.15%, #00032E 90.96%)'

// AGPL-3.0 section 13: network users must be offered the corresponding source.
export const DEFAULT_SOURCE_CODE_URL = 'https://github.com/kr8vka0z/learnhouse'

/**
 * The "Source code" link target, from NEXT_PUBLIC_RR_SOURCE_URL. The value
 * lands in an href, so only absolute http(s) URLs are accepted; anything else
 * (blank, javascript:, protocol-relative) falls back to the default.
 */
export function resolveSourceCodeUrl(raw: string | undefined | null): string {
  const value = (raw || '').trim()
  return /^https?:\/\/[^/]/i.test(value) ? value : DEFAULT_SOURCE_CODE_URL
}
