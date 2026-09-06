import { memo } from 'react'
import { CircleAlert } from 'lucide-react'

import MarkdownRenderer from '../../components/MarkdownRenderer'
import MessageErrorBoundary from '../../components/MessageErrorBoundary'
import { i18nT } from '../../i18n/t'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import type { ChatMessage } from '../../types'

/** The escalation row's `meta`, as `session_escalate` writes it. Every field
 *  is optional here because a rehydrated row may predate one of them. */
export interface EscalationMeta {
  escalation_id?: string
  from_session?: string
  /** The crew member's display name — who acts on the default. */
  member?: string
  deadline?: string | null
  default_action?: string | null
  options?: string[]
  goal?: string | null
}

export function readEscalationMeta(m: ChatMessage): EscalationMeta {
  const meta = (m.meta ?? {}) as Record<string, unknown>
  const options = Array.isArray(meta.options)
    ? (meta.options as unknown[]).filter((o): o is string => typeof o === 'string' && !!o)
    : []
  return {
    escalation_id: typeof meta.escalation_id === 'string' ? meta.escalation_id : undefined,
    from_session: typeof meta.from_session === 'string' ? meta.from_session : undefined,
    member: typeof meta.member === 'string' && meta.member ? meta.member : undefined,
    deadline: typeof meta.deadline === 'string' ? meta.deadline : null,
    default_action: typeof meta.default_action === 'string' ? meta.default_action : null,
    options,
    goal: typeof meta.goal === 'string' ? meta.goal : null,
  }
}

/**
 * The veto deadline WITH its zone. The bell mirror shows the same instant as a
 * UTC clock time; a reader following bell → thread must be able to tell that
 * "09:42 UTC" and the localized time here are one deadline, which a bare local
 * clock reading does not let them do. Falls back to the raw string when the
 * value does not parse.
 */
export function fmtDeadlineWithZone(iso: string, locale?: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  try {
    return new Intl.DateTimeFormat(locale || undefined, {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
      timeZoneName: 'short',
    }).format(d)
  } catch {
    return d.toISOString()
  }
}

/**
 * The PLAIN rendering of a crew member's escalation (`role: escalation`, raised
 * with `session_escalate`): the member's markdown, the decision-critical facts
 * spelled out in words — "unless you reply by <time>, <member> will: <action>"
 * — and the offered options bound to how to answer them (reply in this
 * thread). Registered in the DEFAULT registry so every surface that shows the
 * member's thread draws the question the bell deep-linked to; a host that has
 * the interactive card (option chips, live countdown, state badge — the Crew
 * Members chat profile) replaces this entry by claiming the role.
 *
 * No store reach: this is the default registry's row, and that module must stay
 * usable outside the dashboard's React root.
 */
export default memo(function EscalationNotice({ message }: { message: ChatMessage }) {
  useLanguageGeneration()
  const meta = readEscalationMeta(message)
  const deadline = meta.deadline ? fmtDeadlineWithZone(meta.deadline, document.documentElement.lang || undefined) : ''
  const hasOptions = !!meta.options && meta.options.length > 0
  return (
    <div
      className="self-center w-full max-w-full min-w-0 rounded-md ring-1 ring-inset forced-colors:border ring-warn/30 bg-warn-subtle text-text animate-scale-in"
      data-testid="escalation-notice"
      data-escalation-id={meta.escalation_id}
      role="group"
      aria-label={i18nT('pages.chat.escalationNotice.needs_you')}
    >
      <div className="flex items-start gap-2 px-3 py-2 min-w-0 text-[13px] leading-5">
        <CircleAlert
          size={13}
          className="lucide-inline shrink-0 mt-[calc((1.25rem-1em)/2)] text-warn"
          aria-hidden="true"
        />
        <div className="min-w-0 flex-1 break-words space-y-1">
          <div className="font-medium text-warn">
            {i18nT('pages.chat.escalationNotice.needs_you')}
            {meta.goal && (
              <span className="ml-2 font-normal text-muted">{meta.goal}</span>
            )}
          </div>
          <MessageErrorBoundary rawContent={message.content}>
            <MarkdownRenderer content={message.content} />
          </MessageErrorBoundary>
          {deadline && meta.default_action && (
            <div data-testid="escalation-veto">
              {meta.member
                ? i18nT('pages.chat.escalationNotice.veto', { time: deadline, member: meta.member, action: meta.default_action })
                : i18nT('pages.chat.escalationNotice.veto_generic', { time: deadline, action: meta.default_action })}
            </div>
          )}
          {deadline && !meta.default_action && (
            <div data-testid="escalation-deadline">
              {i18nT('pages.chat.escalationNotice.deadline', { time: deadline })}
            </div>
          )}
          {/* One line, not two: the choices and how to make them are the same
              act. Plain text — nothing here fires on a click. */}
          {hasOptions ? (
            <div data-testid="escalation-options" className="text-muted">
              {i18nT('pages.chat.escalationNotice.options', { list: meta.options!.join(' · ') })}
            </div>
          ) : (
            <div className="text-muted">{i18nT('pages.chat.escalationNotice.reply_hint')}</div>
          )}
        </div>
      </div>
    </div>
  )
})
