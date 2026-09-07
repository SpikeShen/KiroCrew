/**
 * Evidence for the startup feature-intro video modal.
 *
 * Mounts the REAL `StartupVideoModal` through the REAL api client and the REAL
 * placeholder asset, so the frame photographs shipped markup, shipped strings and
 * the actual poster rather than a mock-up. Only the transport is stubbed: one
 * `fetch` shim answers `GET /api/feature-videos/next` with a fixture and swallows
 * the feedback POST, because there is no gateway behind a capture page.
 *
 * Scenes, selected with `?scene=`:
 *
 *   ?scene=share-off — governance says `social_share_enabled: false` (and the
 *     absent-prop case renders identically, since the prop defaults to false).
 *     This is the frame that shows there is no share control in ANY state — not a
 *     greyed one — which is the fail-closed claim the PR makes.
 *
 *   ?scene=share-on — governance granted. The share entry appears beside the
 *     acknowledgement; nothing that reaches an intent URL exists until it is
 *     pressed.
 *
 * `?motion=reduce` forces the reduce-motion branch, so the entrance frame can be
 * shown to be static rather than mid-transition.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createRoot } from 'react-dom/client'

import StartupVideoModal from '../src/components/StartupVideoModal'
import { initI18n } from '../src/i18n'
import { applyFallbackTheme } from '../src/apps/mochi/src/shared/themes'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const shareEnabled = params.get('scene') === 'share-on'

document.documentElement.setAttribute('data-theme', 'kiro-dark')
applyFallbackTheme()
initI18n('en')

/** Realistic copy, so the frame reads like a shipped clip rather than lorem ipsum. */
const FIXTURE = {
  video: {
    id: 'startup-videos-1',
    feature: 'startup-videos',
    title: 'Feature videos',
    description:
      'A short clip introduces each new feature the first time you launch. Watch it '
      + 'once and it never comes back.',
    src: '/app-assets/feature-videos/placeholder.mp4',
    poster: '/app-assets/feature-videos/placeholder.jpg',
    duration_s: 5,
    doc: 'https://example.invalid/docs/feature-videos',
  },
  enabled: true,
}

// Transport-only stub. The component still goes through `api.featureVideoNext`,
// its react-query wiring and its error handling — this just gives that request
// something to resolve against.
const realFetch = window.fetch.bind(window)
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/feature-videos/next')) {
    return Promise.resolve(new Response(JSON.stringify(FIXTURE), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
  }
  if (url.includes('/api/feature-videos/feedback')) {
    return Promise.resolve(new Response(JSON.stringify({ ok: true }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
  }
  return realFetch(input as RequestInfo, init)
}) as typeof window.fetch

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <StartupVideoModal shareEnabled={shareEnabled} onClose={() => {}} />
  </QueryClientProvider>,
)
