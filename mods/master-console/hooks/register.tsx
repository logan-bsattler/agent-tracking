import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { Parked, Question } from '../types'

// The master's console, so Ben clicks instead of typing the prompts he sends
// most, and the master clears itself instead of waiting for his "clear".
//
// 1. Band above the prompt: Next question, Board check, Board cleanup, and one
//    Re-dispatch per task the board says is parked. A press submits the prompt as Ben's.
//    When master has put a question to Ben (coord_present_question), a second
//    row carries one button per option; a press submits the board-built
//    "Answer on <id>: <option>" and closes the question on the board.
// 2. Dashboard buttons (/board, "Ask master", a task page's options) queue the
//    same prompts in coord.db; this mod takes one every 10s while the master
//    is idle and submits it. The dashboard builds the text from a fixed set
//    and stored rows, so a page cannot write the prompt.
// 3. Auto-clear. Fable fills context fast, and waiting on Ben's go left the
//    master stalled at the limit. Over COORD_MASTER_CLEAR tokens (default
//    180k) each prompt carries a note: finish the turn, record the handoff
//    decision, clear_session "self" last. coord-rules allows that clear once a
//    decision was recorded in the same turn.
// 4. Desk. Master runs as two sessions: the worker in the agents folder, and
//    Master - Desk in agents\desk, the one Ben talks to and clears by hand.
//    The desk takes only Ben-facing prompts (Next question, answers, batch
//    answers; the board decides which) and never auto-clears. Its band's
//    Board check, Board cleanup and Re-dispatch queue to the worker instead
//    of running in the desk. With no desk open the worker takes everything.
//    The band shows in the desk only (Ben, 2026-10-28): the worker still takes
//    queued prompts and auto-clears, but Ben clicks from the desk.
// 5. Wake. The worker's poll runs in its own process, which is gone after a
//    self-clear, so its queued prompts waited for an outside message. The
//    desk's tick asks the board (/api/worker/wake) whether a worker prompt has
//    waited 60s with no worker poll and, if so, sends the worker WAKE_MARK:
//    $.session.send first, and the app's send_message when that says the
//    worker is not running (it does not start a stopped session). The worker
//    consumes the marked message (no turn) and takes the prompt at once.
//    Off unless the desk's COORD_WORKER_WAKE names the worker's session id.
// Master and desk only: client sessions load the mod and it does nothing there.

const DASH = 'http://127.0.0.1:8765'
// The master folder itself. Not its children: a side session working in
// agent-tracking is not master, and must neither auto-clear nor take Ben's
// queued prompts.
const MASTER_ROOT = /^c:[\\/]development[\\/]agents[\\/]?$/i
const DESK_ROOT = /^c:[\\/]development[\\/]agents[\\/]desk[\\/]?$/i
// The desktop app opens a scheduled task's session with this wrapper as its
// first prompt (hourly-triage, morning-recap, pull-outlook-meetings).
const SCHEDULED_TEXT = /^\s*<scheduled-task\b/
const FORM = { 'Content-Type': 'application/x-www-form-urlencoded' }
const MAX_BUTTONS = 5
const WAKE_MARK = 'master-console wake:'
const FALLBACK: Record<string, string> = {
  next: 'Next question: rebuild my queue from coord_board and present its head.',
  board: 'Board check: run coord_board, dispatch whatever an agent can do (loose ends, ' +
    'awaiting_redispatch, intents), then reply Done / Next for you.',
  cleanup: 'Board cleanup: run the board cleanup procedure (decision 026f568dc0c3) -- verify open loose ' +
    'ends and stale tasks against outside evidence, close what is done or moot, fold duplicates, ' +
    'keep the rest -- then reply Done / Next for you.',
}

const parked = atom({ plugin: 'master-console', key: 'parked' } as const, [])
const actions = atom({ plugin: 'master-console', key: 'actions' } as const, FALLBACK)
const question = atom({ plugin: 'master-console', key: 'question' } as const, null)

const numOr = (v: string | undefined, d: number) => (v && Number.isFinite(Number(v)) ? Number(v) : d)
const k = (n: number) => `${Math.round(n / 1000)}k`

let isMaster = false  // the worker
let isDesk = false
let busy = false
let tookAt = 'none'  // diagnostic: last prompt this process took
let ctx = 0
let clearAt = 180000
let wakeTo = ''  // the worker's session id, desk only; empty is off
let refresh: () => Promise<void> = async () => {}
let take: () => Promise<void> = async () => {}

// Start-up, run once by whichever hook fires first (2026-11-01). Since
// 10-30 neither master session ran its poll, as if session.start never
// reached this mod, though coord-rules' tool.call hooks still ran. Any
// hook now starts it, and a toast says it is polling.
let started: Promise<void> | undefined
function start($: EngineInterface): Promise<void> {
  return started ??= (async () => {
    const root = await $.session.root()
    isMaster = MASTER_ROOT.test(root)
    isDesk = DESK_ROOT.test(root)
    // The folder alone is not the worker (2026-11-13): triage runs, the
    // recap and any ad hoc session opened here matched too, took minder
    // prompts and were told to auto-clear. When COORD_WORKER_ID names the
    // worker's desktop session id, only that session is the worker. With
    // either value missing the folder rule stands, as before.
    if (isMaster) {
      const workerId = (await $.env.get('COORD_WORKER_ID'))?.trim() ?? ''
      const hostId = (await $.env.get('CLAUDE_CODE_HOST_SESSION_ID'))?.trim() ?? ''
      if (workerId && hostId && workerId !== hostId) isMaster = false
    }
    if (!isMaster && !isDesk) return
    clearAt = numOr(await $.env.get('COORD_MASTER_CLEAR'), 180000)
    if (isDesk) wakeTo = (await $.env.get('COORD_WORKER_WAKE'))?.trim() ?? ''
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
    take = async () => {
      if (busy || (!isMaster && !isDesk)) return
      try {
        const r = await $.http.fetch(`${DASH}/api/prompts/take`, {
          method: 'POST', headers: FORM, body: `taker=${isDesk ? 'desk' : 'master'}`,
        })
        if (!r.ok) return
        const p = (JSON.parse(r.text) as { prompt: { text: string } | null }).prompt
        if (p) {
          busy = true
          tookAt = new Date().toTimeString().slice(0, 5)
          await $.prompt.submit({ text: p.text, asUser: true })
        }
      } catch { /* dashboard down: nothing queued reaches us, nothing lost */ }
    }
    await refresh()
    $.clock.every(10_000, () => {
      void (async () => {
        if (!isMaster && !isDesk) return  // a scheduled session, demoted on its prompt
        await refresh()  // ~20 ms; a question master just presented shows within 10s
        await health($)
        if (wakeTo) await wake($)
        // Diagnostic (2026-11-01): since 10-30 neither session takes prompts.
        // Shows whether this tick runs at all, and whether busy is what stops it.
        const hhmm = () => new Date().toTimeString().slice(0, 5)
        $.ui.status(`console ${isDesk ? 'desk' : 'worker'}: tick ${hhmm()}, ${busy ? 'busy' : 'idle'}, took ${tookAt}`)
        await take()
      })()
    })
    $.ui.toast(`master-console: ${isDesk ? 'desk' : 'worker'} polling for queued prompts`)
  })()
}

// Health (2026-11-13). The dashboard's red banner only helps someone looking
// at /board, and a dead dashboard shows nothing at all. Each tick reads
// /api/health: a new set of problems toasts once, and ~3 minutes of failed
// fetches toasts "dashboard down" once, until it answers again.
const DOWN_AFTER = 180_000
let healthSig = ''
let downSince = 0
let downToasted = false
async function health($: EngineInterface): Promise<void> {
  let text: string
  try {
    const r = await $.http.fetch(`${DASH}/api/health`)
    if (!r.ok) throw new Error('not ok')
    text = r.text
  } catch {
    const t = Date.now()
    if (!downSince) downSince = t
    if (!downToasted && t - downSince >= DOWN_AFTER) {
      downToasted = true
      $.ui.toast('Dashboard down: 127.0.0.1:8765 has not answered for 3 minutes. ' +
        'The minder is not running and queued prompts are not delivered.')
    }
    return
  }
  if (downToasted) $.ui.toast('Dashboard is back.')
  downSince = 0
  downToasted = false
  try {
    const h = JSON.parse(text) as { ok: boolean; problems?: { text?: string }[] }
    const sig = h.ok ? '' : (h.problems ?? []).map(p => p.text ?? '').filter(Boolean).join('; ') || 'not ok'
    if (sig && sig !== healthSig) $.ui.toast(`Board health: ${sig.slice(0, 240)}`)
    healthSig = sig
  } catch { /* not JSON: an older dashboard without /api/health */ }
}

// Desk only: wake the worker when the board says its prompt is stuck. The
// board claims at most one wake per 120s, so a failure toasts that often.
async function wake($: EngineInterface): Promise<void> {
  try {
    const r = await $.http.fetch(`${DASH}/api/worker/wake`, { method: 'POST', headers: FORM, body: '' })
    if (!r.ok) return
    const w = (JSON.parse(r.text) as { wake: { id: number; action: string; waited: number } | null }).wake
    if (!w) return
    const text = `${WAKE_MARK} queued prompt ${w.id} (${w.action}) has waited ${w.waited}s ` +
      'and your poll is not running. No action needed: the mod takes it once you are up.'
    const sent = await $.session.send({ to: { sessionId: wakeTo }, text })
    if (sent.isDelivered) return
    // Not running: only the app's send_message starts a stopped session.
    const res = await $.mcp.call('ccd_session_mgmt', 'send_message', { session_id: wakeTo, message: text })
    const said = res.content.map(b => ('text' in b ? b.text : '') ?? '').join(' ')
    if (res.isError || !/delivered|queued/.test(said)) {
      $.ui.toast(`Worker wake not delivered (${sent.reason}): ${said.slice(0, 160)}`)
    }
  } catch (err) {
    $.ui.toast(`Worker wake failed: ${String(err).slice(0, 160)}`)
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => { await start($); return next(e) })
  on('tool.call', async ($, e, next) => { await start($); return next(e) })

  on('session.measure', async ($, e, next) => {
    if (isMaster) ctx = e.context.tokens ?? ctx
    return next(e)
  })

  // The desk's wake, on the worker: starting this process was its whole job.
  // No turn, so busy stays clear and the prompt is taken now, not next tick.
  on('session.receive', async ($, e, next) => {
    await start($)
    if (!isMaster || e.agentId || !e.text.includes(WAKE_MARK)) return next(e)
    void take()
    return { consumed: 'master-console wake' }
  })

  on('prompt.submit', async ($, e, next) => {
    await start($)
    // Scheduled routines (hourly-triage, morning recap, the Outlook meetings
    // pull) run in the agents folder too, so MASTER_ROOT matched them. Once
    // their turn ended they sat idle with the poll running and took the
    // worker's queued prompts: the minder's "master owes work" landed in a
    // triage session twice on 11-12/13 and never reached master. A session
    // a schedule fires is not the worker. ('sdk' is not a signal: the desktop
    // app may hand Ben's own prompts in that way.)
    if (isMaster && (e.origin?.kind === 'scheduled-trigger' || SCHEDULED_TEXT.test(e.text))) {
      isMaster = false
      $.ui.status(undefined)
    }
    if (!isMaster && !isDesk) return next(e)
    // The same wake, if the app hands it in as a prompt instead.
    if (isMaster && e.text.includes(WAKE_MARK)) {
      void take()
      return { drop: 'master-console: woken; the queued prompt follows' }
    }
    busy = true
    if (isDesk || ctx < clearAt) return next(e)  // Ben clears the desk himself
    const note = `master-console AUTO-CLEAR: context is ${k(ctx)}, over the ${k(clearAt)} threshold. ` +
      'Do this turn\'s work, then coord_record_decision the handoff (live state plus Ben\'s question queue, ' +
      'naming any client you are waiting on), and make clear_session "self" the last call of the turn. ' +
      'Do not ask Ben first; say "cleared, handoff in <decision id>" under Done.'
    return next({ ...e, context: [...(e.context ?? []), note] })
  })

  on('turn.complete', async ($, e, next) => {
    await start($)
    if (e.agentId || (!isMaster && !isDesk)) return next(e)
    busy = false
    await refresh()  // the turn may have presented a question
    return next(e)
  })

  // A self-clear is the last call of its turn, and that turn never reports
  // turn.complete: without this, busy stays set and no queued prompt is taken.
  on('session.end', async ($, e, next) => {
    if ((isMaster || isDesk) && e.reason === 'clear') busy = false
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!isDesk || e.props.hasSurvey) return next(e)
    const list = await read($, parked)
    const texts = await read($, actions)
    const q = await read($, question)
    const { Box, Button, Link, Text } = $.ui.resolve(e)
    const send = (text: string) => () => { void $.prompt.submit({ text, asUser: true }) }
    // The worker's work, queued to it from the desk.
    const work = (action: string, task?: string) => () => {
      void $.http.fetch(`${DASH}/api/prompt`, {
        method: 'POST', headers: { ...FORM, Accept: 'application/json' },
        body: `action=${action}` + (task ? `&task=${task}` : ''),
      }).catch(() => { /* dashboard down: the press is lost, as a page's would be */ })
    }
    const answer = (q: Question, i: number) => () => {
      void (async () => {
        await update($, question, () => null)
        await $.prompt.submit({ text: q.prompts[i], asUser: true })
        try {
          await $.http.fetch(`${DASH}/api/question/close`, {
            method: 'POST',
            headers: FORM,
            body: `question=${q.id}`,
          })
        } catch { /* dashboard down: master's next question closes it anyway */ }
      })()
    }
    return (
      <Box flexDirection="column" rowGap={1}>
        <Box flexDirection="row" flexWrap="wrap" alignItems="center" gap={1}>
          <Button key="next" variant="primary" onPress={send(texts.next ?? FALLBACK.next)}>Next question</Button>
          <Button key="board" onPress={work('board')}>Board check</Button>
          <Button key="cleanup" onPress={work('cleanup')}>Board cleanup</Button>
          {list.slice(0, MAX_BUTTONS).map(d => (
            <Button key={`re-${d.id}`} onPress={work('redispatch', d.id)}>
              Re-dispatch {d.client} <Text dimColor>{d.id.slice(0, 8)}</Text>
            </Button>
          ))}
          {list.length > MAX_BUTTONS ? <Text dimColor>+{list.length - MAX_BUTTONS} parked on /board</Text> : null}
        </Box>
        {q ? <Text>{q.question}</Text> : null}
        {q ? (
          <Box flexDirection="row" flexWrap="wrap" alignItems="center" gap={1}>
            <Text dimColor>
              Answer {q.task_id
                ? <Link href={`${DASH}/board/task/${q.task_id}`}>{q.ref.slice(0, 12)}</Link>
                : q.ref}
              {q.path ? ' · ' : null}
              {q.path ? <Link href={`${DASH}/board/question/${q.id}/file`}>{q.path.split(/[\\/]/).pop()}</Link> : null}:
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
