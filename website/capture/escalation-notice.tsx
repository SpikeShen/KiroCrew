/**
 * Evidence for the escalation row's default rendering (`session_escalate`).
 *
 * THE PROBLEM. A crew member's escalation lands as a `role: escalation` row in
 * the member's DM thread — the surface the bell's "Open" deep-links to. That
 * thread IS a ChatPane, which draws through the default renderer registry; an
 * unclaimed role there rendered as nothing, so the human followed "Radar needs
 * you" to a thread with no visible question.
 *
 * THE SCENE. A short member-thread exchange (user → member → escalation) drawn
 * through the REAL `ChatMessageList` with the REAL default registry — the same
 * path the Members thread takes — so the frame proves the row is claimed by
 * default, not that a component renders in isolation. Three variants of the
 * card: full (deadline + default + options), deadline only, bare. Below it, the
 * bell mirror's body exactly as `_mirror_escalation` composes it, in words.
 *
 *   ?theme=dark|light   ?lang=zh-CN|en
 */
import { createRoot } from 'react-dom/client'

import ChatMessageList from '../src/app-sdk/ChatMessageList'
import { defaultMessageRenderers } from '../src/app-sdk/messageRenderers'
import { initI18n } from '../src/i18n/all'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const lang = params.get('lang') || 'en'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(lang)

const TS = '2026-09-06T09:12:00Z'
const DEADLINE = '2026-09-06T09:42:00+00:00'

const BODY =
  '## Blocked on prod access\n\n' +
  'Tried assuming the deploy role twice; both refused with `AccessDenied`.\n\n' +
  '**Need:** grant `deploy-writer` on the staging account, or tell me to ship from the shared runner instead.'

const thread: ChatMessage[] = [
  { role: 'user', content: 'Ship the fix for #8522 to staging when CI is green.', cls: 'msg msg-u', ts: TS },
  {
    role: 'assistant',
    content: 'CI is green. Starting the staging deploy now.',
    cls: 'msg msg-a',
    ts: TS,
  },
  {
    role: 'escalation',
    content: BODY,
    cls: 'msg msg-escalation',
    ts: TS,
    meta: {
      kind: 'escalation',
      escalation_id: 'esc-3f2a9c1d7e5b4a60',
      from_session: 'member-radar',
      member: 'Radar',
      deadline: DEADLINE,
      default_action: 'ship from the shared runner',
      options: ['Grant the role', 'Ship from the shared runner', 'Hold'],
      goal: 'staging deploy',
      state: 'pending',
      mid: 'm-esc-1',
    },
  },
]

const deadlineOnly: ChatMessage = {
  role: 'escalation',
  content: 'Two flaky tests block the merge; I can retry or skip them. Which?',
  cls: 'msg msg-escalation',
  ts: TS,
  meta: { kind: 'escalation', escalation_id: 'esc-2', deadline: DEADLINE, state: 'pending', mid: 'm-esc-2' },
}

const bare: ChatMessage = {
  role: 'escalation',
  content: 'The vendor API key expired; only you can rotate it.',
  cls: 'msg msg-escalation',
  ts: TS,
  meta: { kind: 'escalation', escalation_id: 'esc-3', state: 'pending', mid: 'm-esc-3' },
}

function Label({ children }: { children: string }) {
  return (
    <div
      style={{
        fontSize: 11,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        opacity: 0.55,
        margin: '18px 0 6px',
        fontFamily: 'ui-sans-serif, system-ui, sans-serif',
      }}
    >
      {children}
    </div>
  )
}

/** The bell body, byte-for-byte what `_mirror_escalation` composes. */
const BELL_TITLE = 'Radar needs you'
const BELL_BODY = [
  'Blocked on prod access',
  'Unless you reply by Sep 6, 09:42 UTC, Radar will: ship from the shared runner',
  'Options: Grant the role · Ship from the shared runner · Hold',
].join('\n')

function Bell() {
  return (
    <div
      data-episode="bell"
      style={{
        border: '1px solid var(--border)',
        borderRadius: 8,
        padding: '10px 12px',
        background: 'var(--card)',
        fontSize: 13,
        lineHeight: '20px',
        maxWidth: 420,
      }}
    >
      <div style={{ fontWeight: 600 }}>{BELL_TITLE}</div>
      <div style={{ whiteSpace: 'pre-wrap', color: 'var(--muted)' }}>{BELL_BODY}</div>
    </div>
  )
}

function Scene() {
  return (
    <div
      data-capture-root
      style={{
        maxWidth: 720,
        margin: '0 auto',
        padding: '20px 24px 28px',
        background: 'var(--bg)',
        color: 'var(--text)',
      }}
    >
      <Label>MEMBER THREAD — the row the bell deep-links to, drawn by the default registry</Label>
      <div data-episode="thread">
        <ChatMessageList messages={thread} running={false} renderers={defaultMessageRenderers} />
      </div>
      <Label>VARIANTS — deadline without a default; neither</Label>
      <div data-episode="variants">
        <ChatMessageList messages={[deadlineOnly, bare]} running={false} renderers={defaultMessageRenderers} />
      </div>
      <Label>BELL — mirror body in words, readable time, options as text</Label>
      <Bell />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Scene />)
