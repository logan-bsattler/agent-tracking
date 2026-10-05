import type { Register, SessionContextUsage, SessionRateLimit } from 'claude-code'

// The in-process successor to coord_mcp.guard. Same policy and thresholds, but
// the figures come from the session itself ($.session.usage / session.measure):
// live context, and the 5-hour and weekly windows with their reset times. The
// Python hook spawned an interpreter per tool call and read Desktop's 15-minute
// samples, so a slow machine skipped it silently and a reset went unseen.

// Tools that survive a hard block, so a session can always check its work back
// in: a teammate's typed result, the lead's decision record, a park.
const EXEMPT = new Set(['coord_complete_task', 'coord_record_decision', 'coord_park_task'])

type Limits = { ctxWarn: number; ctxHard: number; warn5h: number; hard5h: number; warn7d: number; hard7d: number }
type Figures = { context?: SessionContextUsage; rateLimits: SessionRateLimit[] }
type Assessment = { level: 'ok' | 'warn' | 'block'; ctxLevel: 'ok' | 'warn' | 'block'; reasons: string[] }

const RULE = '='.repeat(64)

const k = (n: number) => `${Math.round(n / 1000)}k`

function banner(headline: string, reasons: string[]): string {
  return [RULE, `  !!  ${headline}`, RULE, ...reasons.map(r => `  - ${r}`), RULE,
    'SURFACE THIS NOW. Open your next message with a one-line version of it, before any other ' +
    'content. Then act on it in that same turn rather than starting new work.'].join('\n')
}

function resetText(iso?: string): string {
  if (!iso) return 'soon'
  const d = new Date(iso)
  return 'at ' + d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

// A window whose reset time has passed describes a window that no longer exists.
function live(rl: SessionRateLimit[], kind: string, nowMs: number): SessionRateLimit | undefined {
  const w = rl.find(r => r.kind === kind)
  if (!w) return undefined
  if (w.resetsAt && Date.parse(w.resetsAt) <= nowMs) return undefined
  return w
}

function assess(f: Figures, L: Limits, nowMs: number): Assessment {
  const reasons: string[] = []
  let level: Assessment['level'] = 'ok'
  let ctxLevel: Assessment['ctxLevel'] = 'ok'
  const bump = (to: 'warn' | 'block', msg: string) => {
    reasons.push(msg)
    if (to === 'block' || level === 'ok') level = to
  }

  const ctx = f.context?.tokens ?? 0
  if (ctx >= L.ctxHard) {
    ctxLevel = 'block'
    bump('block', `This session's context is ${k(ctx)} tokens, above the ${k(L.ctxHard)} hard limit. ` +
      'If you hold an open board task, coord_park_task it now, tell the master it needs re-dispatch, ' +
      'then clear yourself. Otherwise run /compact or /clear before continuing.')
  } else if (ctx >= L.ctxWarn) {
    ctxLevel = 'warn'
    bump('warn', `Context is ${k(ctx)} tokens. Keep this turn short and avoid reading large files. If you ` +
      `hold an open board task and cannot finish it in the ${k(L.ctxHard - ctx)} of headroom left, ` +
      `coord_park_task it now. Hard stop at ${k(L.ctxHard)}.`)
  }

  const fh = live(f.rateLimits, 'five_hour', nowMs)
  if (fh) {
    if (fh.percentUsed >= L.hard5h) bump('block', `5-hour limit is at ${fh.percentUsed}%. Stop; it resets ${resetText(fh.resetsAt)}.`)
    else if (fh.percentUsed >= L.warn5h) bump('warn', `5-hour limit at ${fh.percentUsed}%, resets ${resetText(fh.resetsAt)}. Prefer cheap work; do not start anything large.`)
  }
  const sd = live(f.rateLimits, 'seven_day', nowMs)
  if (sd) {
    if (sd.percentUsed >= L.hard7d) bump('block', `Weekly limit is at ${sd.percentUsed}%. Stop; it resets ${resetText(sd.resetsAt)}.`)
    else if (sd.percentUsed >= L.warn7d) bump('warn', `Weekly limit at ${sd.percentUsed}%, resets ${resetText(sd.resetsAt)}.`)
  }
  return { level, ctxLevel, reasons }
}

function statusText(f: Figures): string | undefined {
  const parts: string[] = []
  for (const [kind, label] of [['five_hour', '5h'], ['seven_day', '7d']] as const) {
    const w = f.rateLimits.find(r => r.kind === kind)
    if (w) parts.push(`${label} ${w.percentUsed}%`)
  }
  if (f.context?.tokens) parts.push(`ctx ${k(f.context.tokens)}`)
  return parts.length ? `guard: ${parts.join(' · ')}` : undefined
}

const numOr = (v: string | undefined, dflt: number) => {
  const n = Number(v)
  return Number.isFinite(n) && n > 0 ? n : dflt
}

const pausedUntil = (text: string) => Number(String(text).trim()) * 1000

// Every $ call sits in a hook (the engine reads them off the source), so the
// session's settings are gathered once at session.start, which a reload re-runs.
export const register: Register = on => {
  let figures: Figures = { rateLimits: [] }
  let L: Limits = { ctxWarn: 150000, ctxHard: 300000, warn5h: 75, hard5h: 92, warn7d: 85, hard7d: 97 }
  let coordDir = ''
  let isOff = false
  let calls = 0

  on('session.start', async ($, e, next) => {
    L = {
      ctxWarn: numOr(await $.env.get('COORD_CTX_WARN'), 150000),
      ctxHard: numOr(await $.env.get('COORD_CTX_HARD'), 300000),
      warn5h: numOr(await $.env.get('COORD_WARN_5H'), 75),
      hard5h: numOr(await $.env.get('COORD_HARD_5H'), 92),
      warn7d: numOr(await $.env.get('COORD_WARN_7D'), 85),
      hard7d: numOr(await $.env.get('COORD_HARD_7D'), 97),
    }
    const home = (await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME')) ?? ''
    coordDir = home ? `${home.replace(/\\/g, '/')}/.coord` : ''
    isOff = ['off', '0', 'false'].includes(((await $.env.get('COORD_GUARD')) ?? '').toLowerCase())
    const u = await $.session.usage()
    figures = { context: u.context, rateLimits: u.rateLimits }
    $.ui.status(statusText(figures))
    return next(e)
  })

  // Pushed after every main-thread turn and whenever a window moves a point.
  // Also hand the reading to the coord dashboard (usage.ingest_live_quota).
  on('session.measure', async ($, e, next) => {
    figures = { context: e.context, rateLimits: e.rateLimits }
    $.ui.status(statusText(figures))
    if (coordDir && e.rateLimits.length) {
      const fh = e.rateLimits.find(r => r.kind === 'five_hour')
      const sd = e.rateLimits.find(r => r.kind === 'seven_day')
      try {
        await $.fs.write(`${coordDir}/live-quota.json`, JSON.stringify({
          ts: Math.floor((await $.clock.now()) / 1000),
          five_hour_pct: fh?.percentUsed ?? null, five_hour_reset: fh?.resetsAt ?? null,
          seven_day_pct: sd?.percentUsed ?? null, seven_day_reset: sd?.resetsAt ?? null,
        }))
      } catch { /* the dashboard feed must never break the guard */ }
    }
    return next(e)
  })

  on('tool.call', async ($, e, next) => {
    const nowMs = await $.clock.now()
    if (isOff) return next(e)
    if (coordDir) {
      try {
        if (pausedUntil(await $.fs.read(`${coordDir}/guard-pause`)) > nowMs) return next(e)
      } catch { /* no pause file */ }
    }
    const a = assess(figures, L, nowMs)
    calls += 1
    if (a.level === 'block') {
      const bare = String(e.tool).split('__').pop() ?? ''
      if (!EXEMPT.has(bare)) {
        return { deny: banner('BLOCKED BY THE USAGE GUARD', a.reasons) +
          '\nPause the guard for 30 minutes with: python -m coord_mcp.guard pause 30' }
      }
      const ran = await next(e)
      return ran.deny === undefined
        ? { ...ran, context: [...(ran.context ?? []), banner('BLOCKED -- THIS WRITE IS YOUR LAST ACTION', a.reasons)] }
        : ran
    }
    // Context warnings need action (park and clear): nudge once every ten calls.
    if (a.ctxLevel === 'warn' && calls % 10 === 1) {
      const ran = await next(e)
      return ran.deny === undefined
        ? { ...ran, context: [...(ran.context ?? []), banner('CONTEXT IS RUNNING OUT -- SAVE AND CLEAR', a.reasons)] }
        : ran
    }
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    const nowMs = await $.clock.now()
    if (isOff) return next(e)
    if (coordDir) {
      try {
        if (pausedUntil(await $.fs.read(`${coordDir}/guard-pause`)) > nowMs) return next(e)
      } catch { /* no pause file */ }
    }
    const a = assess(figures, L, nowMs)
    if (a.level === 'ok') return next(e)
    const text = a.ctxLevel === 'ok'
      ? 'Usage guard: ' + a.reasons.join(' ')
      : banner(a.level === 'block' ? 'BLOCKED BY THE USAGE GUARD' : 'CONTEXT IS RUNNING OUT -- SAVE AND CLEAR', a.reasons)
    // A prompt is never dropped: the person may be answering the very question
    // (clear? park?) the block asks. Tool calls are what the block refuses.
    return next({ ...e, context: [...(e.context ?? []), text] })
  })
}
