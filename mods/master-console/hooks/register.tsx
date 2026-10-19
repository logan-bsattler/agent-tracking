import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Parked } from '../types'

// The master's console, so Ben clicks instead of typing the prompts he sends
// most, and the master clears itself instead of waiting for his "clear".
//
// 1. Band above the prompt: Next question, Board check, and one Re-dispatch
//    per task the board says is parked. A press submits the prompt as Ben's.
// 2. Dashboard buttons (/board, "Ask master") queue the same prompts in
//    coord.db; this mod takes one every 10s while the master is idle and
//    submits it. The dashboard builds the text from a fixed set, so a page
//    cannot write the prompt.
// 3. Auto-clear. Fable fills context fast, and waiting on Ben's go left the
//    master stalled at the limit. Over COORD_MASTER_CLEAR tokens (default
//    120k) each prompt carries a note: finish the turn, record the handoff
//    decision, clear_session "self" last. coord-rules allows that clear once a
//    decision was recorded in the same turn.
// Master only: client sessions load the mod and it does nothing there.

const DASH = 'http://127.0.0.1:8765'
const MASTER_ROOT = /^c:[\/]development[\/]agents([\/]|$)/i
const MAX_BUTTONS = 5
const FALLBACK: Record<string, string> = {
  next: 'Next question: rebuild my queue from coord_board and present its head.',
  board: 'Board check: run coord_board, dispatch whatever an agent can do (loose ends, ' +
    'awaiting_redispatch, intents), then reply Done / Next for you.',
}

const parked = atom({ plugin: 'master-console', key: 'parked' } as const, [])
const actions = atom({ plugin: 'master-console', key: 'actions' } as const, FALLBACK)

const numOr = (v: string | undefined, d: number) => (v && Number.isFinite(Number(v)) ? Number(v) : d)
const k = (n: number) => `${Math.round(n / 1000)}k`

export const register: Register = on => {
  let isMaster = false
  let busy = false
  let ctx = 0
  let clearAt = 120000

  on('session.start', async ($, e, next) => {
    isMaster = MASTER_ROOT.test(await $.session.root())
    if (!isMaster) return next(e)
    clearAt = numOr(await $.env.get('COORD_MASTER_CLEAR'), 120000)
    ctx = (await $.session.usage()).context.tokens ?? 0
    let tick = 0
    const refresh = async () => {
      try {
        const r = await $.http.fetch(`${DASH}/api/parked`)
        if (!r.ok) return
        const body = JSON.parse(r.text) as { parked: Parked[]; actions?: Record<string, string> }
        await update($, parked, () => body.parked)
        if (body.actions) await update($, actions, () => ({ ...FALLBACK, ...body.actions }))
      } catch { /* dashboard down: keep the last list; the band still has Next and Board */ }
    }
    await refresh()
    $.clock.every(10_000, () => {
      void (async () => {
        if (++tick % 6 === 0) await refresh()
        if (busy) return
        try {
          const r = await $.http.fetch(`${DASH}/api/prompts/take`, { method: 'POST' })
          if (!r.ok) return
          const p = (JSON.parse(r.text) as { prompt: { text: string } | null }).prompt
          if (p) {
            busy = true
            await $.prompt.submit({ text: p.text, asUser: true })
          }
        } catch { /* dashboard down: nothing queued reaches us, nothing lost */ }
      })()
    })
    return next(e)
  })

  on('session.measure', async ($, e, next) => {
    if (isMaster) ctx = e.context.tokens ?? ctx
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    if (!isMaster) return next(e)
    busy = true
    if (ctx < clearAt) return next(e)
    const note = `master-console AUTO-CLEAR: context is ${k(ctx)}, over the ${k(clearAt)} threshold. ` +
      'Do this turn\'s work, then coord_record_decision the handoff (live state plus Ben\'s question queue, ' +
      'naming any client you are waiting on), and make clear_session "self" the last call of the turn. ' +
      'Do not ask Ben first; say "cleared, handoff in <decision id>" under Done.'
    return next({ ...e, context: [...(e.context ?? []), note] })
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId || !isMaster) return next(e)
    busy = false
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!isMaster || e.props.hasSurvey) return next(e)
    const list = await read($, parked)
    const texts = await read($, actions)
    const { Box, Button, Text } = $.ui.resolve(e)
    const send = (text: string) => () => { void $.prompt.submit({ text, asUser: true }) }
    return (
      <Box flexDirection="row" flexWrap="wrap" gap={1}>
        <Button key="next" variant="primary" onPress={send(texts.next ?? FALLBACK.next)}>Next question</Button>
        <Button key="board" onPress={send(texts.board ?? FALLBACK.board)}>Board check</Button>
        {list.slice(0, MAX_BUTTONS).map(d => (
          <Button key={`re-${d.id}`} onPress={send(d.prompt)}>
            Re-dispatch {d.client} <Text dimColor>{d.id.slice(0, 8)}</Text>
          </Button>
        ))}
        {list.length > MAX_BUTTONS ? <Text dimColor>+{list.length - MAX_BUTTONS} parked on /board</Text> : null}
      </Box>
    )
  })
}
