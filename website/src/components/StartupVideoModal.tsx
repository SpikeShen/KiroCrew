import { lazy, Suspense, useCallback, useId, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { motion, useReducedMotion } from 'framer-motion'
import { Share2, X } from 'lucide-react'

import { api, type FeatureVideo, type FeatureVideoNext } from '../api/client'
import { useDialogFocusTrap } from '../hooks/useDialogFocusTrap'
import { i18nT } from '../i18n/t'

/**
 * The feature-intro video shown once at startup.
 *
 * Mounted only after `startupVideoGate` has ruled that this launch is free — that
 * module owns "may anything open at all", this file owns "what is it and what
 * does watching it mean". The split is what keeps the policy testable without the
 * dashboard and keeps this chunk lazy.
 *
 * The clip retires PERMANENTLY, by one of two verdicts, and both are one-way:
 * `seen` when the user watches most of it or presses the acknowledgement, and
 * `dismissed` when they close it. Neither is a snooze — the backend stops
 * offering a clip after either — so there is no "remind me" affordance here to
 * imply otherwise.
 *
 * The clip's BYTES are not fetched until the user asks for them: `preload="none"`
 * plus a `poster` means the browser draws the still and waits for a play. A
 * launch that shows no video (the overwhelming majority) costs one small JSON
 * response and no media at all.
 */

const LazyShareMessageModal = lazy(() => import('../pages/chat/share/ShareMessageModal'))

/** Fraction of the clip that counts as watched. */
const SEEN_AT = 0.8

export interface StartupVideoModalProps {
  /**
   * The `capabilities.social_share` governance answer, as `/api/dashboard/config`
   * reports it in `social_share_enabled`.
   *
   * Defaults to FALSE so a caller that forgets to wire it hides sharing rather
   * than exposing it — the same fail-closed posture as `AssistantMessage`. This
   * component adds no scope and no flag of its own: sharing a feature clip is the
   * same act, under the same policy, as sharing a reply.
   */
  shareEnabled?: boolean
  /** Called once a verdict has been dispatched and the modal should go away. */
  onClose: () => void
}

export default function StartupVideoModal({ shareEnabled = false, onClose }: StartupVideoModalProps) {
  const { data, isError } = useQuery<FeatureVideoNext>({
    queryKey: ['feature-video-next'],
    queryFn: () => api.featureVideoNext(),
    // One request per launch. The answer cannot change underneath us: only this
    // component's own verdict retires a clip, and by then it is closing.
    staleTime: Infinity,
    gcTime: Infinity,
    // A 404 is the expected answer from a gateway that predates the endpoint, and
    // asking again recovers nothing — so no retry, and the error path renders
    // nothing rather than an empty dialog.
    retry: false,
  })

  const video = data?.video ?? null
  const open = !isError && !!video && data?.enabled === true

  // Nothing is on screen until there is a clip AND the feature is on, so the
  // dialog itself lives in its own component below. That component MOUNTS at the
  // moment the dialog appears, which is what the shared focus trap needs: the
  // trap moves focus in on ITS mount and never re-runs, so a trap mounted here —
  // while this component still returns null — would aim at a dialog that does not
  // exist yet and leave focus on the page behind the overlay for good.
  if (!open || !video) return null

  return <OpenStartupVideoModal video={video} shareEnabled={shareEnabled} onClose={onClose} />
}

/** The dialog itself. Mounted only while there is a clip to show. */
function OpenStartupVideoModal({ video, shareEnabled, onClose }: {
  video: FeatureVideo
  shareEnabled: boolean
  onClose: () => void
}) {
  const reduceMotion = useReducedMotion()
  const dialogRef = useRef<HTMLDivElement | null>(null)
  const reactId = useId()
  const titleId = `${reactId}-title`
  const descId = `${reactId}-desc`
  const [shareOpen, setShareOpen] = useState(false)
  /** One verdict per clip. `timeupdate` fires several times a second, and the
   *  user can still press a button after crossing the threshold, so without this
   *  the same decision is posted repeatedly. */
  const verdictSent = useRef(false)

  /**
   * Record the verdict. Does NOT close -- that separation is the whole point.
   *
   * Crossing the watched threshold is a fact about the clip, not a request to take it
   * off screen. Closing here snatched the dialog away mid-playback and, because the
   * verdict is permanent, made the final stretch unwatchable for good.
   *
   * Deliberately not awaited: nothing the user does next should wait on this write.
   * The rejection is NOT discarded either -- `api/client.ts`'s `j` helper calls
   * `recordError` on every non-2xx, so a refused verdict is already in the error
   * journal, with its endpoint, status and backend code, before this handler runs.
   * The `catch` exists only to keep that expected rejection from surfacing as an
   * unhandled one; the journal is the record.
   */
  const recordVerdict = useCallback((status: 'seen' | 'dismissed') => {
    if (verdictSent.current) return
    verdictSent.current = true
    void api.featureVideoFeedback(video.id, status).catch(() => {})
  }, [video])

  /**
   * Record the verdict AND close -- for the moments the user is actually done: the
   * clip ended, or they pressed something. A verdict already recorded at the 80% mark
   * wins, so finishing a clip you acknowledged early does not post twice.
   */
  const settle = useCallback((status: 'seen' | 'dismissed') => {
    recordVerdict(status)
    onClose()
  }, [recordVerdict, onClose])

  const dismiss = useCallback(() => settle('dismissed'), [settle])

  /**
   * Close and record NOTHING -- the backdrop's behaviour.
   *
   * A stray click on the scrim is not a decision about the clip, and `dismissed` is
   * permanent: one misclick used to retire a clip the user never watched, with no way
   * back. Closing silently leaves the verdict unwritten, so the backend offers it
   * again next launch. A verdict already recorded at the 80% mark is untouched -- this
   * only declines to write a NEW one.
   */
  const closeWithoutVerdict = useCallback(() => { onClose() }, [onClose])

  // Focus in, focus restore on close, Escape, and the Tab/Shift+Tab trap — the
  // shared implementation the other hand-rolled dialogs use. Suspended while the
  // share dialog is up so one Escape does not close both layers.
  useDialogFocusTrap(dialogRef, dismiss, { enabled: !shareOpen })

  const onTimeUpdate = (e: React.SyntheticEvent<HTMLVideoElement>) => {
    const el = e.currentTarget
    // Prefer the element's own metadata, and fall back to the catalog duration for
    // the frames before it loads (and for a source whose duration never resolves).
    const total = Number.isFinite(el.duration) && el.duration > 0 ? el.duration : video.duration_s
    if (!total || !Number.isFinite(total)) return
    // Record only. The clip keeps playing and the dialog stays put -- the user decides
    // when it goes away.
    if (el.currentTime / total >= SEEN_AT) recordVerdict('seen')
  }

  // Share caption: the feature's own words, then its doc link when it has one.
  // Both halves come from the API, so there is no copy here to translate — and no
  // URL is composed, the backend's `doc` is passed through exactly as given.
  // Joined rather than interpolated so the caption carries no authored string.
  const shareBody = [video.description, video.doc].filter(Boolean).join('\n\n')

  return (
    // Presentation, not a control: an ARIA button may not contain interactive
    // descendants, and focus never lands on the scrim, so a keydown handler here
    // would be unreachable. Escape covers keyboard dismissal -- and unlike this
    // scrim it RECORDS a verdict, because pressing it is a deliberate act where a
    // stray click is not.
    <div
      className="fixed inset-0 z-50 bg-bg/80 backdrop-blur-sm flex items-center justify-center"
      role="presentation"
      onClick={e => { if (e.target === e.currentTarget) closeWithoutVerdict() }}
    >
      <motion.div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        // Named by its own heading and described by the blurb, so the clip's real
        // title is announced rather than a generic label.
        aria-labelledby={titleId}
        aria-describedby={descId}
        // `initial={false}` is framer-motion's own way to say "start where you
        // end": under reduce-motion the dialog is simply present, with no
        // entrance to sit through.
        initial={reduceMotion ? false : { opacity: 0, y: 8, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={reduceMotion ? { duration: 0 } : { duration: 0.22, ease: 'easeOut' }}
        className="bg-card border border-border rounded-xl shadow-xl w-[560px] max-w-[92vw] flex flex-col overflow-hidden outline-none"
      >
        <div className="flex items-center justify-between px-4 py-2.5 border-b border-border bg-bg-elevated">
          <span className="text-sm font-semibold text-text">
            {i18nT('components.startupVideoModal.whats_new')}
          </span>
          <button
            type="button"
            className="text-muted hover:text-text cursor-pointer bg-transparent border-none"
            onClick={dismiss}
            aria-label={i18nT('components.startupVideoModal.close')}
          >
            <X size={16} />
          </button>
        </div>

        {/* eslint-disable-next-line jsx-a11y/media-has-caption -- the contract
            (`GET /api/feature-videos/next`) carries no captions track, and the
            placeholder clip has no audio to caption. A narrated clip DOES need
            one, so the field is a known gap recorded on the PR rather than a
            guessed-at addition to the API. */}
        <video
          data-testid="startup-video"
          className="w-full bg-bg aspect-video"
          src={video.src}
          poster={video.poster}
          // Names the player after the clip it plays, so the control is not an
          // unlabelled surface in the tab order.
          aria-label={video.title}
          controls
          playsInline
          // No autoplay and no preload: the bytes arrive when the user asks for
          // them, so an unwatched clip costs nothing beyond the poster.
          preload="none"
          onTimeUpdate={onTimeUpdate}
          onEnded={() => settle('seen')}
        />

        <div className="px-4 py-3 text-sm text-text">
          <p id={titleId} className="font-semibold text-text-strong">{video.title}</p>
          <p id={descId} className="mt-1 text-[13px] text-muted">{video.description}</p>
        </div>

        <div className="flex flex-wrap items-center justify-end gap-2 px-4 py-2.5 border-t border-border bg-bg-elevated">
          {/* Governance is a RENDER gate here, not a disabled state: an entry the
              policy has not granted should not be on screen at all, so there is no
              greyed button to explain and nothing on the page that could reach an
              intent URL. */}
          {shareEnabled && (
            <button
              type="button"
              data-testid="startup-video-share"
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-border text-text hover:border-border-strong bg-transparent cursor-pointer"
              onClick={() => setShareOpen(true)}
            >
              <Share2 size={14} className="lucide-inline" />
              {i18nT('components.startupVideoModal.share')}
            </button>
          )}
          <button
            type="button"
            className="px-3 py-1.5 text-sm rounded-md bg-accent text-accent-fg hover:opacity-90 cursor-pointer"
            onClick={() => settle('seen')}
          >
            {i18nT('components.startupVideoModal.got_it')}
          </button>
        </div>
      </motion.div>

      {/* The existing chat share card, reused whole: the clip's title takes the
          question slot and its blurb plus doc link the excerpt slot, which is the
          shape that card already renders.
          Gated on `shareOpen` ALONE, matching `AssistantMessage.tsx`. The card
          guards itself: it re-reads permission from a ref AFTER its export await,
          and that ref only refreshes while the card keeps rendering. Unmounting it
          on a revoked policy freezes the ref at `true`, and an export already in
          flight then opens the social composer anyway — the very navigation the
          revocation exists to stop. So the card stays and is handed the live
          answer, which it uses to show a notice and withdraw its own actions.
          Fail-closed still holds at the ENTRY: the Share button above is gated, so
          no new share can START once the policy says no. */}
      {shareOpen && (
        <Suspense fallback={null}>
          <LazyShareMessageModal
            onClose={() => setShareOpen(false)}
            messageText={shareBody}
            prevUserText={video.title}
            shareEnabled={shareEnabled}
            // The card's own copy describes a chat reply and a question. Here the
            // subject is a feature clip and its title, so the two strings that name
            // the shared thing are replaced. Everything else in that dialog is about
            // the sharing mechanics and reads correctly as-is.
            copy={{
              description: i18nT('components.startupVideoModal.share_description'),
              includeQuestion: i18nT('components.startupVideoModal.share_include_title'),
            }}
          />
        </Suspense>
      )}
    </div>
  )
}
