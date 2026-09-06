/**
 * Screenshot runner for capture/escalation-notice.html.
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6823 --strictPort
 *   node scripts/capture-escalation-notice.mjs http://127.0.0.1:6823 <outdir>
 *
 * Captures the member-thread scene in both themes, element-scoped, and
 * self-checks the claim the frame is evidence for: the `escalation` row is
 * CLAIMED by the default registry (three cards for three rows), the veto
 * window is a sentence with no raw ISO timestamp and no emoji, and the options
 * read as text.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6823'
const OUT = process.argv[3] || '../temp-screenshots/escalation-notice'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['light', 'dark']) {
  const ctx = await browser.newContext({
    viewport: { width: 780, height: 1600 },
    deviceScaleFactor: 1,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  try {
    await page.goto(`${BASE}/capture/escalation-notice.html?theme=${theme}&lang=en`, {
      waitUntil: 'networkidle',
    })
    await page.waitForSelector('[data-capture-root]', { timeout: 30000 })
    await page.waitForSelector('[data-testid="escalation-notice"]', { timeout: 30000 })
    await page.waitForTimeout(600)
    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)

    const probe = await page.evaluate(() => {
      const cards = [...document.querySelectorAll('[data-testid="escalation-notice"]')]
      const thread = document.querySelector('[data-episode="thread"]')
      const veto = thread?.querySelector('[data-testid="escalation-veto"]')?.textContent ?? ''
      const options = thread?.querySelector('[data-testid="escalation-options"]')?.textContent ?? ''
      const text = document.body.innerText
      return {
        cards: cards.length,
        threadCards: thread ? thread.querySelectorAll('[data-testid="escalation-notice"]').length : 0,
        veto,
        options,
        rawIso: /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(text),
        emoji: /[\u23F3\u21AA\u2610]/.test(text),
        deadlineOnly: document.querySelectorAll('[data-episode="variants"] [data-testid="escalation-deadline"]').length,
        bareVeto: document.querySelectorAll('[data-episode="variants"] [data-testid="escalation-veto"]').length,
      }
    })
    if (probe.cards !== 3) throw new Error(`expected 3 escalation cards, got ${probe.cards}`)
    if (probe.threadCards !== 1) throw new Error(`expected the thread to draw exactly 1 card, got ${probe.threadCards}`)
    if (!/^Unless you reply by .+, Radar will: ship from the shared runner$/.test(probe.veto))
      throw new Error(`veto line not a sentence: ${JSON.stringify(probe.veto)}`)
    if (!probe.options.startsWith('To answer, type one of these as a reply in this thread: ')) throw new Error(`options not bound to the typed-reply path: ${probe.options}`)
    if (probe.rawIso) throw new Error('a raw ISO timestamp leaked into the rendered text')
    if (probe.emoji) throw new Error('a veto/options glyph leaked into the rendered text')
    if (probe.deadlineOnly !== 1) throw new Error(`expected 1 deadline-only line, got ${probe.deadlineOnly}`)
    if (probe.bareVeto !== 0) throw new Error(`a variant without a default drew a veto line`)

    const el = page.locator('[data-capture-root]')
    await el.screenshot({ path: `${OUT}/escalation-notice-${theme}.png` })
    console.log(`ok ${theme}: 3 cards claimed by default, veto in words, options as text`)
  } catch (e) {
    failed = 1
    console.error(`FAIL ${theme}:`, e.message)
  } finally {
    await ctx.close()
  }
}

await browser.close()
process.exit(failed)
