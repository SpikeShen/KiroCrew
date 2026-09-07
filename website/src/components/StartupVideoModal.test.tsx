import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'

import { renderWithProviders } from '../test/helpers'
import { i18nT } from '../i18n/t'
import { api } from '../api/client'
import type { FeatureVideo } from '../api/client'
import StartupVideoModal from './StartupVideoModal'

vi.mock('../api/client', () => {
  class MockApiError extends Error {}
  return {
    ApiError: MockApiError,
    api: {
      featureVideoNext: vi.fn(),
      featureVideoFeedback: vi.fn(),
    },
  }
})

/**
 * The share card is stubbed rather than rendered: it drags in `html-to-image` and
 * a canvas export path that has nothing to do with this modal's behaviour, and
 * the PROP CONTRACT between the two is already enforced by `tsc` (the real
 * `ShareMessageModalProps` is required at the call site). What these tests own is
 * whether the section is reachable at all.
 */
vi.mock('../pages/chat/share/ShareMessageModal', () => ({
  default: ({ messageText, prevUserText, copy, shareEnabled }: {
    messageText: string
    prevUserText?: string
    copy?: { description?: string; includeQuestion?: string }
    shareEnabled: boolean
  }) => (
    <div data-testid="stub-share-modal">
      <span data-testid="stub-share-title">{prevUserText}</span>
      <span data-testid="stub-share-body">{messageText}</span>
      {/* Echoed so a test can assert the host passed surface-appropriate wording
          rather than letting the chat defaults through. */}
      <span data-testid="stub-share-copy-description">{copy?.description}</span>
      <span data-testid="stub-share-copy-include">{copy?.includeQuestion}</span>
      {/* The real card withdraws its own actions when this is false. A test
          asserts the host keeps FEEDING it the live answer instead of pulling
          the card out from under an export already in flight. */}
      <span data-testid="stub-share-enabled">{String(shareEnabled)}</span>
      {/* Stands in for the real card's intent buttons, so a test can assert on
          the same testids the shipped card uses. */}
      <button data-testid="share-x">x</button>
      <button data-testid="share-linkedin">linkedin</button>
    </div>
  ),
}))

const mockedApi = vi.mocked(api)

const clip: FeatureVideo = {
  id: 'vid-1',
  feature: 'startup-videos',
  title: 'Feature videos',
  description: 'A short clip introduces each new feature.',
  src: '/app-assets/feature-videos/placeholder.mp4',
  poster: '/app-assets/feature-videos/placeholder.jpg',
  duration_s: 10,
  doc: 'https://example.invalid/docs/feature-videos',
}

const dialog = () => screen.queryByRole('dialog')
const video = () => screen.queryByTestId('startup-video') as HTMLVideoElement | null

/**
 * Mount and settle. The open path spans two async hops (the query resolving,
 * then the render it enables), so a single flush can sample between them and let
 * a "stays closed" assertion pass without ever having had the chance to open.
 */
async function mount(props: Partial<React.ComponentProps<typeof StartupVideoModal>> = {}) {
  const onClose = props.onClose ?? vi.fn()
  const rendered = renderWithProviders(
    <StartupVideoModal {...props} onClose={onClose} />,
  )
  for (let i = 0; i < 6; i++) {
    await act(async () => { await new Promise(r => setTimeout(r, 5)) })
  }
  return { ...rendered, onClose }
}

beforeEach(() => {
  mockedApi.featureVideoNext.mockReset()
  mockedApi.featureVideoFeedback.mockReset()
  mockedApi.featureVideoFeedback.mockResolvedValue({ ok: true } as never)
  mockedApi.featureVideoNext.mockResolvedValue({ video: clip, enabled: true } as never)
})

describe('StartupVideoModal — when it renders nothing', () => {
  it('renders nothing when the backend has no video to offer', async () => {
    // The steady state for most launches, and NOT an error: every clip has been
    // seen or dismissed already.
    mockedApi.featureVideoNext.mockResolvedValue({ video: null, enabled: true } as never)
    await mount()
    expect(dialog()).not.toBeInTheDocument()
  })

  it('renders nothing when the feature is disabled, even with a video attached', async () => {
    // `enabled` is the operator kill switch and outranks the payload; a disabled
    // install that still names a clip must stay silent.
    mockedApi.featureVideoNext.mockResolvedValue({ video: clip, enabled: false } as never)
    await mount()
    expect(dialog()).not.toBeInTheDocument()
  })

  it('renders nothing when the request fails (404 from an older gateway)', async () => {
    mockedApi.featureVideoNext.mockRejectedValue(new Error('HTTP 404'))
    await mount()
    expect(dialog()).not.toBeInTheDocument()
  })

  it('posts no verdict on any of those paths', async () => {
    mockedApi.featureVideoNext.mockResolvedValue({ video: null, enabled: true } as never)
    await mount()
    // A launch that showed nothing must not retire anything — otherwise the clip
    // is burned without the user ever seeing it.
    expect(mockedApi.featureVideoFeedback).not.toHaveBeenCalled()
  })
})

describe('StartupVideoModal — the clip', () => {
  it('opens with the clip title, blurb, poster and source', async () => {
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByText(clip.title)).toBeInTheDocument()
    expect(screen.getByText(clip.description)).toBeInTheDocument()
    const el = video()
    expect(el).toBeInTheDocument()
    expect(el).toHaveAttribute('src', clip.src)
    expect(el).toHaveAttribute('poster', clip.poster)
  })

  it('names and describes itself for assistive tech', async () => {
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    const d = dialog() as HTMLElement
    expect(d).toHaveAttribute('aria-modal', 'true')
    // Named by the clip's own heading rather than a generic label, so the
    // announcement says which feature this is about.
    const labelId = d.getAttribute('aria-labelledby')
    const descId = d.getAttribute('aria-describedby')
    expect(labelId).toBeTruthy()
    expect(descId).toBeTruthy()
    expect(document.getElementById(labelId as string)).toHaveTextContent(clip.title)
    expect(document.getElementById(descId as string)).toHaveTextContent(clip.description)
  })

  it('moves keyboard focus into the dialog once it appears', async () => {
    // The dialog's first commit renders NOTHING: the clip arrives from a query, so
    // there is no dialog element yet. Focus entry must survive that gap. If it is
    // wired to the component's mount instead of the dialog's appearance, an
    // aria-modal overlay lands on screen with focus still on the page behind it --
    // a keyboard user is left tabbing through controls the overlay covers.
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    await waitFor(() => {
      expect((dialog() as HTMLElement).contains(document.activeElement)).toBe(true)
    })
  })

  it('keeps an open share card mounted when the policy is revoked mid-share', async () => {
    // The card guards ITSELF: it re-reads permission from a ref after its export
    // await, and that ref only refreshes while the card is still rendering. Pulling
    // the card out on a policy flip freezes the ref at `true`, so an export already
    // in flight goes on to open the social composer -- the exact navigation the
    // revocation was meant to stop. So the card stays, and is handed the new answer.
    mockedApi.featureVideoNext.mockResolvedValue({ video: clip, enabled: true })
    const { rerender } = await mount({ shareEnabled: true })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('startup-video-share'))
    await waitFor(() => expect(screen.getByTestId('stub-share-modal')).toBeInTheDocument())

    rerender(<StartupVideoModal shareEnabled={false} onClose={vi.fn()} />)
    await act(async () => { await new Promise(r => setTimeout(r, 5)) })

    expect(screen.getByTestId('stub-share-modal')).toBeInTheDocument()
    expect(screen.getByTestId('stub-share-enabled')).toHaveTextContent('false')
    // The ENTRY still fails closed: no new share can be started under a policy
    // that now says no.
    expect(screen.queryByTestId('startup-video-share')).not.toBeInTheDocument()
  })

  it('never fetches the clip bytes before the user asks for them', async () => {
    // The whole cost argument for showing this at startup: the poster is drawn
    // and the media waits. `preload` must be "none" and there must be no
    // autoplay, or every launch downloads a video nobody watched.
    await mount()
    await waitFor(() => expect(video()).toBeInTheDocument())
    expect(video()).toHaveAttribute('preload', 'none')
    expect(video()).not.toHaveAttribute('autoplay')
    expect(video()).toHaveAttribute('controls')
  })

  it('does not render a video element at all while there is nothing to show', async () => {
    // The strongest form of "no fetch before open": no element exists to fetch
    // with, so a disabled or empty response cannot touch the network.
    mockedApi.featureVideoNext.mockResolvedValue({ video: null, enabled: true } as never)
    await mount()
    expect(video()).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: i18nT('components.startupVideoModal.close') }))
      .not.toBeInTheDocument()
  })
})

describe('StartupVideoModal — verdicts are permanent', () => {
  it('posts `seen` at 80% and KEEPS PLAYING — the dialog does not close itself', async () => {
    const { onClose } = await mount()
    await waitFor(() => expect(video()).toBeInTheDocument())
    const el = video() as HTMLVideoElement
    // happy-dom does not decode media, so `duration` never resolves; the component
    // falls back to the catalog's `duration_s`, which is the same path a stream with
    // unknown duration takes.
    el.currentTime = 7.9 // 79% — under the line
    fireEvent.timeUpdate(el)
    expect(mockedApi.featureVideoFeedback).not.toHaveBeenCalled()

    el.currentTime = 8 // exactly 80%
    fireEvent.timeUpdate(el)
    await waitFor(() => expect(mockedApi.featureVideoFeedback)
      .toHaveBeenCalledWith(clip.id, 'seen'))

    // The regression guard. Crossing the threshold is a fact about the clip, not a
    // request to take it off screen: closing here snatched the dialog away
    // mid-playback, and because `seen` is permanent the last 20% became unwatchable
    // forever. An earlier version of this test asserted the opposite and so defended
    // the bug.
    expect(onClose).not.toHaveBeenCalled()
    expect(dialog()).toBeInTheDocument()
    expect(video()).toBeInTheDocument()
  })

  it('closes when the clip reaches its end, without posting a second verdict', async () => {
    const { onClose } = await mount()
    await waitFor(() => expect(video()).toBeInTheDocument())
    const el = video() as HTMLVideoElement
    el.currentTime = 8
    fireEvent.timeUpdate(el)
    await waitFor(() => expect(mockedApi.featureVideoFeedback).toHaveBeenCalledTimes(1))

    fireEvent.ended(el)
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    // The 80% mark already recorded it; finishing must not post again.
    expect(mockedApi.featureVideoFeedback).toHaveBeenCalledTimes(1)
  })

  it('posts exactly one verdict however many timeupdates arrive', async () => {
    await mount()
    await waitFor(() => expect(video()).toBeInTheDocument())
    const el = video() as HTMLVideoElement
    el.currentTime = 9
    // `timeupdate` fires several times a second; a per-event post would record
    // the same decision dozens of times.
    for (let i = 0; i < 5; i++) fireEvent.timeUpdate(el)
    await waitFor(() => expect(mockedApi.featureVideoFeedback).toHaveBeenCalledTimes(1))
  })

  it('posts `seen` when the acknowledgement is pressed', async () => {
    const { onClose } = await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.startupVideoModal.got_it') }))
    await waitFor(() => expect(mockedApi.featureVideoFeedback)
      .toHaveBeenCalledWith(clip.id, 'seen'))
    expect(onClose).toHaveBeenCalled()
  })

  it('posts `dismissed` when closed with the X', async () => {
    const { onClose } = await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.startupVideoModal.close') }))
    await waitFor(() => expect(mockedApi.featureVideoFeedback)
      .toHaveBeenCalledWith(clip.id, 'dismissed'))
    expect(onClose).toHaveBeenCalled()
  })

  it('posts `dismissed` when closed with Escape', async () => {
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(mockedApi.featureVideoFeedback)
      .toHaveBeenCalledWith(clip.id, 'dismissed'))
  })

  it('closes WITHOUT a verdict when the backdrop is clicked', async () => {
    // A stray click on the scrim is not a decision about the clip, and `dismissed` is
    // permanent -- one misclick used to retire a clip the user never watched, with no
    // re-watch path. Closing silently leaves the verdict unwritten so the backend
    // offers it again next launch. An earlier version of this test asserted the
    // opposite and pinned the behaviour that was overturned.
    const { onClose } = await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    const scrim = document.querySelector('[role="presentation"]') as HTMLElement
    fireEvent.click(scrim)

    await waitFor(() => expect(onClose).toHaveBeenCalled())
    expect(mockedApi.featureVideoFeedback).not.toHaveBeenCalled()
  })

  it('does not undo a verdict already recorded at 80% when the backdrop closes it', async () => {
    // Closing without a verdict declines to write a NEW one; it must not retract the
    // `seen` the threshold already sent.
    await mount()
    await waitFor(() => expect(video()).toBeInTheDocument())
    const el = video() as HTMLVideoElement
    el.currentTime = 8
    fireEvent.timeUpdate(el)
    await waitFor(() => expect(mockedApi.featureVideoFeedback)
      .toHaveBeenCalledWith(clip.id, 'seen'))

    fireEvent.click(document.querySelector('[role="presentation"]') as HTMLElement)
    // Still exactly the one `seen`: no second call, and no `dismissed` overwriting it.
    expect(mockedApi.featureVideoFeedback).toHaveBeenCalledTimes(1)
    expect(mockedApi.featureVideoFeedback).not.toHaveBeenCalledWith(clip.id, 'dismissed')
  })

  it('still posts `dismissed` for the deliberate closes — X and Escape', async () => {
    // The distinction the fix rests on: a stray scrim click is not a decision, while
    // pressing X or Escape is. Those keep recording.
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(mockedApi.featureVideoFeedback)
      .toHaveBeenCalledWith(clip.id, 'dismissed'))
  })

  it('does not dismiss when a click lands INSIDE the dialog', async () => {
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(dialog() as HTMLElement)
    expect(mockedApi.featureVideoFeedback).not.toHaveBeenCalled()
  })

  it('closes even when the verdict write fails', async () => {
    // The write is fire-and-forget on purpose: a slow or broken gateway must not
    // leave the user trapped behind a dialog they have finished with.
    mockedApi.featureVideoFeedback.mockRejectedValue(new Error('gateway down'))
    const { onClose } = await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.startupVideoModal.got_it') }))
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })
})

describe('StartupVideoModal — share governance fails closed', () => {
  const shareLabel = () => i18nT('components.startupVideoModal.share')

  /** Nothing on the page that could reach an X or LinkedIn intent URL. */
  function expectNoIntentSurface() {
    expect(screen.queryByTestId('startup-video-share')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: shareLabel() })).not.toBeInTheDocument()
    expect(screen.queryByTestId('stub-share-modal')).not.toBeInTheDocument()
    expect(screen.queryByTestId('share-x')).not.toBeInTheDocument()
    expect(screen.queryByTestId('share-linkedin')).not.toBeInTheDocument()
  }

  it('hides the whole share section when the prop is ABSENT', async () => {
    // A forgotten wire must hide sharing, not expose it — the prop defaults to
    // false for exactly this case.
    await mount()
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expectNoIntentSurface()
  })

  it('hides the whole share section when governance says false', async () => {
    // `social_share_enabled: false` from /api/dashboard/config. Hidden, NOT
    // disabled: a greyed button is still an element that explains a policy the
    // user cannot act on.
    await mount({ shareEnabled: false })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expectNoIntentSurface()
  })

  it('renders no disabled share control either', async () => {
    await mount({ shareEnabled: false })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    // Guards against a refactor that "keeps the affordance visible" by disabling
    // it: with the policy off there must be no share control in any state.
    const disabled = screen.queryAllByRole('button').filter(b => b.hasAttribute('disabled'))
    expect(disabled).toHaveLength(0)
    expect(document.body.textContent).not.toContain(shareLabel())
  })

  it('offers the share entry only once governance says true', async () => {
    await mount({ shareEnabled: true })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    expect(screen.getByTestId('startup-video-share')).toBeInTheDocument()
    // Still nothing that reaches an intent until the user opens the card.
    expect(screen.queryByTestId('share-x')).not.toBeInTheDocument()
  })

  it('shares the clip title and its blurb plus doc link through the existing card', async () => {
    await mount({ shareEnabled: true })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('startup-video-share'))
    await waitFor(() => expect(screen.getByTestId('stub-share-modal')).toBeInTheDocument())
    expect(screen.getByTestId('stub-share-title')).toHaveTextContent(clip.title)
    const body = screen.getByTestId('stub-share-body')
    expect(body).toHaveTextContent(clip.description)
    expect(body).toHaveTextContent(clip.doc as string)
  })

  it('shares title and blurb alone for a clip with no doc link', async () => {
    const noDoc = { ...clip, doc: undefined }
    mockedApi.featureVideoNext.mockResolvedValue({ video: noDoc, enabled: true } as never)
    await mount({ shareEnabled: true })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('startup-video-share'))
    await waitFor(() => expect(screen.getByTestId('stub-share-modal')).toBeInTheDocument())
    expect(screen.getByTestId('stub-share-body')).toHaveTextContent(noDoc.description)
  })

  it('gives the share card video wording instead of the chat defaults', async () => {
    // The reused card describes a chat reply and a question by default. Sharing a
    // feature clip through it unchanged made the dialog assert something false.
    await mount({ shareEnabled: true })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('startup-video-share'))
    await waitFor(() => expect(screen.getByTestId('stub-share-modal')).toBeInTheDocument())

    expect(screen.getByTestId('stub-share-copy-description'))
      .toHaveTextContent(i18nT('components.startupVideoModal.share_description'))
    expect(screen.getByTestId('stub-share-copy-include'))
      .toHaveTextContent(i18nT('components.startupVideoModal.share_include_title'))
    // And the chat defaults must not leak through.
    expect(document.body.textContent).not.toContain(i18nT('pages.chat.share.description'))
    expect(document.body.textContent).not.toContain(i18nT('pages.chat.share.include_question'))
  })

  it('opening the share card does not retire the clip', async () => {
    // Sharing is not an acknowledgement: the user may still watch it afterwards.
    await mount({ shareEnabled: true })
    await waitFor(() => expect(dialog()).toBeInTheDocument())
    fireEvent.click(screen.getByTestId('startup-video-share'))
    await waitFor(() => expect(screen.getByTestId('stub-share-modal')).toBeInTheDocument())
    expect(mockedApi.featureVideoFeedback).not.toHaveBeenCalled()
  })
})
