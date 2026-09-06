import { render, screen } from '@testing-library/react'
import EscalationNotice, { fmtDeadlineWithZone, readEscalationMeta } from '../pages/chat/EscalationNotice'
import type { ChatMessage } from '../types'

const row = (meta: Record<string, unknown>, content = '## Blocked on prod access\n\nNeed role Z.'): ChatMessage =>
  ({ role: 'escalation', content, cls: 'msg msg-escalation', meta }) as ChatMessage

describe('EscalationNotice', () => {
  it('names the member and the zoned deadline in the veto sentence, and binds the options to the reply path', () => {
    render(
      <EscalationNotice
        message={row({
          kind: 'escalation',
          escalation_id: 'esc-1',
          member: 'Radar',
          deadline: '2030-01-01T09:30:00+00:00',
          default_action: 'Push A',
          options: ['Push A', 'Hold'],
          goal: 'triage',
        })}
      />,
    )
    expect(screen.getByTestId('escalation-notice')).toHaveAttribute('data-escalation-id', 'esc-1')
    expect(screen.getByText('Blocked on prod access')).toBeInTheDocument()
    // Inaction-means-consent is the decision-critical fact: a sentence naming
    // WHO acts, with a zoned time — the same vocabulary the bell uses — not a
    // glyph beside a raw timestamp.
    const veto = screen.getByTestId('escalation-veto')
    expect(veto).toHaveTextContent(/^Unless you reply by .+, Radar will: Push A$/)
    expect(veto).not.toHaveTextContent('the member')
    expect(veto).not.toHaveTextContent('2030-01-01T09:30:00')
    expect(veto.textContent).toMatch(/UTC|GMT|[A-Z]{2,5}T\b|[+-]\d{1,2}(:\d{2})?/)
    // The choices and how to make them are one line; nothing here is a button.
    expect(screen.getByTestId('escalation-options')).toHaveTextContent(
      'To answer, type one of these as a reply in this thread: Push A · Hold',
    )
    expect(screen.queryByText('Reply in this thread to answer.')).toBeNull()
    expect(screen.getByText('triage')).toBeInTheDocument()
  })

  it('falls back to "the member" when the row predates the member name', () => {
    render(
      <EscalationNotice
        message={row({ escalation_id: 'esc-0', deadline: '2030-01-01T09:30:00+00:00', default_action: 'Push A' })}
      />,
    )
    expect(screen.getByTestId('escalation-veto')).toHaveTextContent(/, the member will: Push A$/)
  })

  it('states a bare deadline when no default was declared, and the reply hint when there are no options', () => {
    const { unmount } = render(
      <EscalationNotice message={row({ escalation_id: 'esc-2', deadline: '2030-01-01T09:30:00+00:00' })} />,
    )
    expect(screen.getByTestId('escalation-deadline')).toHaveTextContent(/^Reply by .+; with no reply, nothing happens and the request stays open.$/)
    expect(screen.queryByTestId('escalation-veto')).toBeNull()
    expect(screen.queryByTestId('escalation-options')).toBeNull()
    expect(screen.getByText('Reply in this thread to answer.')).toBeInTheDocument()
    unmount()
    render(<EscalationNotice message={row({ escalation_id: 'esc-3' })} />)
    expect(screen.queryByTestId('escalation-deadline')).toBeNull()
    expect(screen.queryByTestId('escalation-veto')).toBeNull()
  })

  it('formats the deadline with its zone and falls back to the raw string', () => {
    expect(fmtDeadlineWithZone('2030-01-01T09:30:00+00:00', 'en-US')).toMatch(/2030/)
    expect(fmtDeadlineWithZone('2030-01-01T09:30:00+00:00', 'en-US')).toMatch(/UTC|GMT|[A-Z]{2,5}T\b|[+-]\d/)
    expect(fmtDeadlineWithZone('not a date')).toBe('not a date')
  })

  it('reads a rehydrated row defensively', () => {
    expect(readEscalationMeta(row({ options: ['a', 3, '', 'b'], deadline: 7, goal: null, member: '' }))).toEqual({
      escalation_id: undefined,
      from_session: undefined,
      member: undefined,
      deadline: null,
      default_action: null,
      options: ['a', 'b'],
      goal: null,
    })
    expect(readEscalationMeta({ role: 'escalation', content: 'x', cls: '' } as ChatMessage).options).toEqual([])
  })
})
