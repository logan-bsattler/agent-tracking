import { atom, read, update } from 'claude-code'
import type { Register } from 'claude-code'

import type { Parked, Question } from '../types'

// The master's console, so Ben clicks instead of typing the prompts he sends
// most, and the master clears itself instead of waiting for his "clear".
//
// 1. Band above the prompt: Next question, Board check, and one Re-dispatch
//    per task the board says is parked. A press submits the prompt as Ben's.
//    When master has put a question to Ben (coord_present_question), a second
//    row carries one button per option; a press submits the board-built
//    "Answer on <id>: <option>" and closes the question on the board.
// 2. Dashboard buttons (/board, "Ask master", a task page's options) queue the
//    same prompts in coord.db; this mod takes one every 10s while the master
//    is idle and submits it. The dashboard builds the text from a fixed set
//    and stored rows, so a page cannot write the prompt.
// 3. Auto-clear. Fable fills context fast, and waiting on Ben's go left the
//    master stalled at the limit. Over COORD_MASTER_CLEAR tokens (default
//    120k) each prompt carries a note: finish the turn, record the handoff
//    decision, clear_session "self" last. coord-rules allows that clear once a
//    decision was recorded in the same turn.
// Master only: client sessions load the mod and it does nothing there.

const DASH = 'http://127.0.0.1:8765'
// The master folder itself. Not its children: a side session working in
// agent-tracking is not master, and must neither auto-clear nor take Ben's
// queued prompts.
const MASTER_ROOT = /^c:[\\/]development[\\/]agents[\\/]?$/i
const MAX_BUTTONS = 5
const FALLBACK: Record<string, string> = {
  next: 'Next question: rebuild my queue from coord_board and present its head.',
  board: 'Board check: run coord_board, dispatch whatever an agent can do (loose ends, ' +
    'awaiting_redispatch, intents), then reply Done / Next for you.',
}

const parked = atom({ plugin: 'master-console', key: 'parked' } as const, [])
const actions = atom({ plugin: 'master-console', key: 'actions' } as const, FALLBACK)
const question = atom({ plugin: 'master-console', key: 'question' } as const, null)

const numOr = (v: string | undefined, d: number) => (v && Number.isFinite(Number(v)) ? Number(v) : d)
const k = (n: number) => `${Math.round(n / 1000)}k`

export const register: Register = on => {
  let isMaster = false
  let busy = false
  let ctx = 0
  let clearAt = 120000
  let refresh: () => Promise<void> = async () => {}

  on('session.start', async ($, e, next) => {
    isMaster = MASTER_ROOT.test(await $.session.root())
    if (!isMaster) return next(e)
    clearAt = numOr(await $.env.get('COORD_MASTER_CLEAR'), 120000)
    ctx = (await $.session.usage()).context.tokens ?? 0
    refresh = async () => {
      try {
        const r = await $.http.fetch(`${DASH}/api/parked`)
        if (!r.ok) return
        const body = JSON.parse(r.text) as {
          parked: Parked[]; actions?: Record<string, string>; question?: Question | null
        }
        await update($, parked, () => body.parked)
        await update($, question, () => body.question ?? null)
        if (body.actions) await update($, actions, () => ({ ...FALLBACK, ...body.actions }))
      } catch { /* dashboard down: keep the last list; the band still has Next and Board */ }
    }
    await refresh()
    $.clock.every(10_000, () => {
      void (async () => {
        await refresh()  // ~20 ms; a question master just presented shows within 10s
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
    await refresh()  // the turn may have presented a question
    return next(e)
  })

  // A self-clear is the last call of its turn, and that turn never reports
  // turn.complete: without this, busy stays set and no queued prompt is taken.
  on('session.end', async ($, e, next) => {
    if (isMaster && e.reason === 'clear') busy = false
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!isMaster || e.props.hasSurvey) return next(e)
    const list = await read($, parked)
    const texts = await read($, actions)
    const q = await read($, question)
    const { Box, Button, Link, Text } = $.ui.resolve(e)
    const send = (text: string) => () => { void $.prompt.submit({ text, asUser: true }) }
    const answer = (q: Question, i: number) => () => {
      void (async () => {
        await update($, question, () => null)
        await $.prompt.submit({ text: q.prompts[i], asUser: true })
        try {
          await $.http.fetch(`${DASH}/api/question/close`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: `question=${q.id}`,
          })
        } catch { /* dashboard down: master's next question closes it anyway */ }
      })()
    }
    return (
      <Box flexDirection="column" rowGap={1}>
        <Box flexDirection="row" flexWrap="wrap" alignItems="center" gap={1}>
          <Button key="next" variant="primary" onPress={send(texts.next ?? FALLBACK.next)}>Next question</Button>
          <Button key="board" onPress={send(texts.board ?? FALLBACK.board)}>Board check</Button>
          {list.slice(0, MAX_BUTTONS).map(d => (
            <Button key={`re-${d.id}`} onPress={send(d.prompt)}>
              Re-dispatch {d.client} <Text dimColor>{d.id.slice(0, 8)}</Text>
            </Button>
          ))}
          {list.length > MAX_BUTTONS ? <Text dimColor>+{list.length - MAX_BUTTONS} parked on /board</Text> : null}
        </Box>
        {q ? (
          <Box flexDirection="row" flexWrap="wrap" alignItems="center" gap={1}>
            <Text dimColor>
              Answer <Link href={`${DASH}/board/task/${q.task_id}`}>{q.ref.slice(0, 12)}</Link>:
            </Text>
            {q.options.map((o, i) => (
              <Button key={`ans-${q.id}-${i}`} variant={i === q.recommended ? 'primary' : undefined}
                onPress={answer(q, i)}>{o}</Button>
            ))}
          </Box>
        ) : null}
      </Box>
    )
  })
}
