/**
 * useEscalationIndex — the backend's per-member conversation index, keyed by
 * escalation id, for a member DM thread.
 *
 * The escalation card's state used to be SIMULATED over the pane's hydrated
 * window (~50 rows). An older pending escalation outside that window plus a
 * free-text human reply made the client's free-text rule answer the VISIBLE
 * card while the backend — the authority — still had both pending. This hook
 * reads `GET /api/members/{slug}/conversation` and hands the renderer the
 * server's own record per id; `deriveEscalationState` defers to it when the
 * id is known and falls back to the simulation otherwise.
 *
 * Backed by the shared react-query cache under `escalationIndexQueryKey(slug)`
 * so a remounted pane (the Members page re-opening a thread, a hydration
 * remount) starts from the last known index instead of the simulation while
 * the refetch is in flight. Fetch policy:
 * - once on mount / slot change (a cached index is shown at once and refreshed
 *   when older than STALE_TIME_MS);
 * - again whenever `replyTick` changes — the host passes a cheap dependency
 *   that moves when the slot gains a `user` or `escalation` row;
 * - again whenever `slotsTick` changes — the host passes a value riding the
 *   dashboard's `slots` websocket push (the slot's `needs_you` flag), so the
 *   backend's decision lands without a second subscription;
 * - every POLL_INTERVAL_MS while `pollWhile(states)` / `pollWhilePending`
 *   says at least one visible card is pending, off otherwise;
 * - `refresh()` on demand (the card after a confirmed send, the 45 s valve).
 *
 * Concurrent triggers dedupe onto the request in flight (react-query). A slot
 * change is a key change: the previous slot's request is aborted and its late
 * answer can never land on the new slot; the in-flight request is aborted on
 * unmount. `states` is null until the first load succeeds and on a non-member
 * slot — the caller then simulates. A REFETCH that fails keeps the last good
 * index (and reports `error`): a record the server did write beats a
 * client-side guess that only sees the hydrated window, and a failed refresh
 * is a network fact, not new information about the escalation.
 */
import { useCallback, useEffect, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, type MemberConversationIndex } from '../../api/client'
import type { ChatMessage } from '../../types'
import { errMessage } from '../../utils/thunkError'
import { escalationDeadlineMs, type EscalationIndexEntry } from './escalationState'

/** Slot keys of member DM threads: `member-<slug>`. */
const MEMBER_SLOT_PREFIX = 'member-'

/** Cadence of the safety-net poll while a visible card is pending. */
export const POLL_INTERVAL_MS = 20_000

/**
 * How long a cached index counts as fresh on (re)mount. Short: the index is
 * the authority for a live decision, so a remount older than this refetches
 * — but still shows the cached record meanwhile rather than the simulation.
 */
const STALE_TIME_MS = 5_000

/** The ONE spelling of the per-member conversation index query key. */
export const escalationIndexQueryKey = (slug: string) => ['members', slug, 'conversation'] as const

export type EscalationIndexStates = Record<string, EscalationIndexEntry>

export interface UseEscalationIndexOptions {
  /** Cheap dependency: changes when the slot's messages gain a user/escalation row. */
  replyTick?: string | number
  /** Cheap dependency riding the `slots` websocket push (e.g. the slot's `needs_you`). */
  slotsTick?: unknown
  /** Poll every POLL_INTERVAL_MS while true. */
  pollWhilePending?: boolean
  /**
   * Alternative to `pollWhilePending` that sees the current index: evaluated
   * every render (keep it cheap, e.g. `anyEscalationPending`). Wins when set.
   */
  pollWhile?: (states: EscalationIndexStates | null) => boolean
  /** Test seam; defaults to the api client. */
  fetcher?: (slug: string, signal: AbortSignal) => Promise<MemberConversationIndex>
}

export interface EscalationIndex {
  /** Per-id index entries, or null when no index is available (fall back to simulation). */
  states: EscalationIndexStates | null
  /**
   * Why `states` is null when the index request FAILED (the message of the
   * query's error; an empty string for a message-less failure). Null while
   * loading, on success, and on a non-member slot — those are not failures.
   * The host renders it through `ErrorNotice`, so a simulation forced by an
   * outage is visible rather than silently standing in for the record.
   */
  error: string | null
  /** Ask for a fresh read now (deduped onto any request in flight). */
  refresh: () => void
}

/** The member slug a slot key names, or undefined for a non-member slot. */
export function memberSlugOf(slotKey: string | undefined): string | undefined {
  if (!slotKey || !slotKey.startsWith(MEMBER_SLOT_PREFIX)) return undefined
  const slug = slotKey.slice(MEMBER_SLOT_PREFIX.length)
  return slug || undefined
}

/** Index the payload's escalation entries by id; malformed entries are skipped. */
export function indexEntries(payload: MemberConversationIndex | null | undefined): EscalationIndexStates {
  const out: EscalationIndexStates = {}
  const entries = Array.isArray(payload?.entries) ? payload!.entries : []
  for (const entry of entries) {
    if (!entry || typeof entry !== 'object') continue
    if (entry.type !== undefined && entry.type !== 'escalation') continue
    if (typeof entry.id !== 'string' || !entry.id) continue
    out[entry.id] = entry
  }
  return out
}

const defaultFetcher = (slug: string, signal: AbortSignal) => api.memberConversation(slug, signal)

/**
 * True when at least one `escalation` row is still open: per the index entry
 * when it knows the id, else per the row's own deadline. Drives the poll.
 */
export function anyEscalationPending(
  messages: readonly ChatMessage[],
  states: EscalationIndexStates | null,
  now: number,
): boolean {
  for (const m of messages) {
    if (m.role !== 'escalation') continue
    const id = m.meta?.escalation_id
    const entry = states && typeof id === 'string' && id ? states[id] : undefined
    if (entry) {
      if (entry.state === 'pending') return true
      continue
    }
    const deadline = escalationDeadlineMs(m)
    if (deadline === null || deadline > now) return true
  }
  return false
}

export function useEscalationIndex(
  slotKey: string | undefined,
  { replyTick, slotsTick, pollWhilePending = false, pollWhile, fetcher }: UseEscalationIndexOptions = {},
): EscalationIndex {
  const slug = memberSlugOf(slotKey)
  const fetcherRef = useRef(fetcher ?? defaultFetcher)
  fetcherRef.current = fetcher ?? defaultFetcher
  // The poll predicate is read by react-query's interval callback, outside
  // render: keep the latest through a ref.
  const pollRef = useRef({ pollWhile, pollWhilePending })
  pollRef.current = { pollWhile, pollWhilePending }

  const { data, isError, error: queryError, refetch } = useQuery({
    queryKey: escalationIndexQueryKey(slug ?? ''),
    // Reading `signal` is what lets react-query abort this request when the
    // observer leaves the key (slot change) or unmounts.
    queryFn: ({ signal }) => fetcherRef.current(slug!, signal),
    enabled: !!slug,
    staleTime: STALE_TIME_MS,
    // Function form: re-evaluated against the query's own latest data after
    // every fetch and on every options update, so the poll switches off the
    // moment the index (or the host) says nothing is pending.
    refetchInterval: (query) => {
      const { pollWhile: predicate, pollWhilePending: flag } = pollRef.current
      const raw = query.state.data
      const current = raw === undefined ? null : indexEntries(raw)
      const polling = predicate ? predicate(current) : flag
      return polling ? POLL_INTERVAL_MS : false
    },
  })

  // No index → the caller simulates: on a non-member slot, and until the first
  // load succeeds. A failed REFETCH does not throw the cached record away —
  // react-query keeps `data` across an error, and a stale-but-authoritative
  // `pending` is safer than the window simulation, which can read a truncated
  // transcript as "answered" and hide the controls while the deadline default
  // takes the person's decision. The failure is surfaced through `error`.
  const states = useMemo<EscalationIndexStates | null>(
    () => (!slug || data === undefined ? null : indexEntries(data)),
    [slug, data],
  )
  // Only a FAILED request is an error: a non-member slot never asks, and a
  // load in flight is not a failure yet.
  const error = slug && isError ? errMessage(queryError) : null

  // `refetch` ignores `enabled`, so a non-member slot must not reach it.
  // `cancelRefetch: false` dedupes onto the request in flight instead of
  // restarting it.
  const slugRef = useRef(slug)
  slugRef.current = slug
  const refresh = useCallback(() => {
    if (!slugRef.current) return
    void refetch({ cancelRefetch: false })
  }, [refetch])

  // Refetch triggers: a new user/escalation row, or a slots push. The mount
  // fetch belongs to useQuery; skip this effect's initial run.
  const first = useRef(true)
  useEffect(() => {
    if (first.current) { first.current = false; return }
    refresh()
  }, [replyTick, slotsTick, refresh])

  return { states, error, refresh }
}
