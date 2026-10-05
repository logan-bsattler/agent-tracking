import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Snapshot, Tokens } from '../types'

// /usage in a pane that stays open: limits with resets, session cost, the
// token breakdown and cache hit rate, model mix, and what fills the context.
// Refreshed when the engine measures (each turn, each limit point) and once a
// minute so the reset countdowns move while idle. Token totals count the turns
// since this pane loaded; cost is the engine's own running total.
// Every $ call sits directly in a hook: the engine reads them off the source.

const PANE = 'usage-pane'
const snap = atom({ plugin: 'usage-pane', key: 'snap' } as const, null)
const ZERO: Tokens = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, turns: 0, byModel: {} }
const tokens = atom({ plugin: 'usage-pane', key: 'tokens' } as const, ZERO)

const LABEL: Record<string, string> = { five_hour: 'Session (5h)', seven_day: 'Weekly', spend_limit: 'Spend' }

type UsageLike = {
  rateLimits: Snapshot['limits']
  context: {
    tokens?: number
    window: number
    percent?: number
    breakdown?: { categories: { name: string; tokens: number; isDeferred: boolean }[] }
  }
  cost?: { usd: number }
}

function toSnap(u: UsageLike, at: number, prev: Snapshot | null): Snapshot {
  const cats = u.context.breakdown?.categories
  return {
    limits: u.rateLimits, ctxTokens: u.context.tokens, ctxWindow: u.context.window,
    ctxPercent: u.context.percent, costUsd: u.cost?.usd, at,
    categories: cats
      ? cats.filter(c => !c.isDeferred && c.tokens > 0).map(c => ({ name: c.name, tokens: c.tokens }))
          .sort((a, b) => b.tokens - a.tokens).slice(0, 8)
      : prev?.categories ?? [],
  }
}

function until(iso: string | undefined, nowMs: number): string {
  if (!iso) return ''
  const mins = Math.max(0, Math.round((Date.parse(iso) - nowMs) / 60000))
  if (mins < 60) return `resets in ${mins} min`
  if (mins < 24 * 60) return `resets in ${Math.floor(mins / 60)} hr ${mins % 60} min`
  return 'resets ' + new Date(iso).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' })
}

function bar(pct: number, width: number): string {
  const n = Math.max(0, Math.min(width, Math.round((pct / 100) * width)))
  return '█'.repeat(n) + '░'.repeat(width - n)
}

// The coord dashboard (coord_mcp.usage serve, kept up by a logon task). Opened
// in the default browser by the master session at start instead of the pane,
// at most once per DASH_EVERY_MS so a run of clears does not stack up tabs.
// Client sessions open nothing. /dashboard opens it on demand anywhere.
const DASH_URL = 'http://127.0.0.1:8765/'
const DASH_EVERY_MS = 8 * 60 * 60 * 1000
const MASTER_ROOT = /^c:[\\/]development[\\/]agents([\\/]|$)/i

const fmt = (n?: number) =>
  n === undefined ? '-' : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'usage-pane', description: 'Open the usage pane' })
    await $.command.register({ name: 'dashboard', description: 'Open the coord dashboard in the browser' })
    const prev = await read($, snap)
    const first = toSnap(await $.session.usage({ breakdown: 'summary' }), await $.clock.now(), prev)
    await update($, snap, () => first)
    $.clock.every(60_000, () => {
      void (async () => {
        const p = await read($, snap)
        const s = toSnap(await $.session.usage(), await $.clock.now(), p)
        await update($, snap, () => s)
      })()
    })
    if (MASTER_ROOT.test(await $.session.root())) {
      const nowMs = await $.clock.now()
      const last = Number((await $.store.get('dashboardOpenedAt')) ?? 0)
      if (nowMs - last > DASH_EVERY_MS) {
        try {
          await $.process.run(['cmd', '/c', 'start', '', DASH_URL])
          await $.store.set('dashboardOpenedAt', nowMs)
        } catch { /* no browser to hand it to: say nothing, /dashboard still works */ }
      }
    }
    return next(e)
  })

  on('command.run', { command: 'dashboard' }, async $ => {
    await $.process.run(['cmd', '/c', 'start', '', DASH_URL])
    return { text: `Dashboard opened: ${DASH_URL}` }
  })

  on('command.run', { command: 'usage-pane' }, async $ => {
    const prev = await read($, snap)
    const s = toSnap(await $.session.usage({ breakdown: 'summary' }), await $.clock.now(), prev)
    await update($, snap, () => s)
    await $.ui.open({ id: PANE, title: 'Usage' })
    return { text: 'Usage pane opened.' }
  })

  // `summary` estimates the context categories locally: no requests sent.
  on('session.measure', async ($, e, next) => {
    const prev = await read($, snap)
    const s = toSnap(await $.session.usage({ breakdown: 'summary' }), await $.clock.now(), prev)
    await update($, snap, () => s)
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const u = e.usage
    if (u) {
      await update($, tokens, t => {
        const cur = t ?? ZERO
        const out = u.output_tokens ?? 0
        return {
          input: cur.input + (u.input_tokens ?? 0),
          output: cur.output + out,
          cacheRead: cur.cacheRead + (u.cache_read_input_tokens ?? 0),
          cacheWrite: cur.cacheWrite + (u.cache_creation_input_tokens ?? 0),
          turns: cur.turns + 1,
          byModel: { ...cur.byModel, [u.model]: (cur.byModel[u.model] ?? 0) + out },
        }
      })
    }
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    const s = await read($, snap)
    const t = (await read($, tokens)) ?? ZERO
    if (!s) return <Text dimColor>No usage reading yet.</Text>
    const width = Math.max(10, Math.min(40, (e.props.bodyColumns ?? 40) - 8))
    const ctxPct = s.ctxPercent ?? (s.ctxTokens ? Math.round((s.ctxTokens / s.ctxWindow) * 100) : 0)
    const inAll = t.input + t.cacheRead + t.cacheWrite
    const hit = inAll ? Math.round((t.cacheRead / inAll) * 100) : undefined
    const outAll = Object.values(t.byModel).reduce((a, b) => a + b, 0)
    const models = Object.entries(t.byModel).sort((a, b) => b[1] - a[1])
    const catMax = s.categories[0]?.tokens ?? 1
    const cost = s.costUsd === undefined ? '-' : `$${s.costUsd.toFixed(2)}`
    return (
      <Box flexDirection="column">
        {s.limits.length === 0 && <Text dimColor>No limit reading yet.</Text>}
        {s.limits.map(l => (
          <Box flexDirection="column">
            <Text>{LABEL[l.kind] ?? l.kind}  <Text bold>{l.percentUsed}%</Text>  <Text dimColor>{until(l.resetsAt, s.at)}</Text></Text>
            <Text color={l.percentUsed >= 90 ? 'red' : l.percentUsed >= 75 ? 'yellow' : undefined}>{bar(l.percentUsed, width)}</Text>
          </Box>
        ))}
        <Text> </Text>
        <Text bold>This session</Text>
        <Text>Cost <Text bold>{cost}</Text>   Cache hit <Text bold>{hit === undefined ? '-' : `${hit}%`}</Text>   Turns {t.turns}</Text>
        {models.map(([m, n]) => (
          <Text dimColor>{m}  {outAll ? Math.round((n / outAll) * 100) : 0}% of output</Text>
        ))}
        <Text> </Text>
        <Text bold>Tokens <Text dimColor>(since pane loaded)</Text></Text>
        <Text>Input {fmt(t.input)}   Output {fmt(t.output)}</Text>
        <Text>Cache read {fmt(t.cacheRead)}   Cache write {fmt(t.cacheWrite)}</Text>
        <Text> </Text>
        <Text bold>Context  {fmt(s.ctxTokens)} of {fmt(s.ctxWindow)} <Text dimColor>({ctxPct}%)</Text></Text>
        <Text color={(s.ctxTokens ?? 0) >= 150_000 ? 'yellow' : undefined}>{bar(ctxPct, width)}</Text>
        {s.categories.map(c => (
          <Text dimColor>{bar((c.tokens / catMax) * 100, Math.max(6, Math.floor(width / 3)))} {c.name} {fmt(c.tokens)}</Text>
        ))}
        <Text> </Text>
        <Text dimColor>as of {new Date(s.at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}</Text>
      </Box>
    )
  })
}
