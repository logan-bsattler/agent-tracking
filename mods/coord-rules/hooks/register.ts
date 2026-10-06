import type { Register } from 'claude-code'

// The master session's two rules that have broken in practice, held in code
// rather than in CLAUDE.md, so they survive a /clear or compaction.
//
// 1. Clear guard. clear_session "self" fires as the turn ends, so a warning in
//    the same turn is read in a session that no longer remembers it (9/30,
//    10/01). It is refused unless the operator's own prompt in this turn says
//    "clear". A message from a client session never counts. The rule is the
//    master's alone: the mod loads in every session, and a client's
//    park-and-clear is the protocol with nobody typing in that session (10/05:
//    MAG and LNK parked, could not clear, and re-parked on re-dispatch). Scoped
//    by the session's root rather than a parked-this-turn flag, which a reload
//    mid-turn would lose. Every self-clear decision is logged to
//    ~/.coord/coord-rules.log, so the next failure leaves evidence.
// 2. Dispatch check. coord_create_task writes a row and wakes nobody; a task
//    whose id never reaches send_message sits open forever. Each one created
//    this turn and not yet named in a send_message is flagged at turn end, and
//    carried into the next prompt so it lands on Waiting or gets delivered.

// Origins that are Ben himself: the composer, Remote Control, the desktop host.
const OPERATOR = new Set(['composer', 'bridge', 'sdk'])

// "clear" as its own word, not cleared/clearing, and not negated just before it.
const saysClear = (text: string) =>
  /\bclear\b/i.test(text) && !/\b(don'?t|do not|not|never|no)\s+(\w+\s+)?clear\b/i.test(text)

const TASK_ID = /"task_id"\s*:\s*\\?"([0-9a-f]{12})\\?"/

const bare = (tool: string) => String(tool).split('__').pop() ?? ''

// The master session's folder; client sessions live under OneDrive Documents.
const MASTER_ROOT = /^c:[\\/]development[\\/]agents([\\/]|$)/i
const LOG_KEEP = 200

export const register: Register = on => {
  let operatorGo = false
  // task id -> client key, for tasks created and not yet named in a send_message
  const pending = new Map<string, string>()
  let carried: string[] = []

  on('prompt.submit', async ($, e, next) => {
    if (OPERATOR.has(e.origin.kind)) operatorGo = saysClear(e.text)
    if (!carried.length) return next(e)
    const note = 'Dispatch check: created last turn and never sent to the client: ' +
      carried.join(', ') + '. Deliver each with send_message, or name it on Waiting as held.'
    carried = []
    return next({ ...e, context: [...(e.context ?? []), note] })
  })

  on('tool.call', async ($, e, next) => {
    if (e.agentId) return next(e)
    const name = bare(e.tool)

    if (name === 'clear_session') {
      const target = String((e as Record<string, unknown>).session_id ?? '')
      if (target !== 'self') return next(e)
      const root = await $.session.root()
      const isMaster = MASTER_ROOT.test(root)
      const allow = !isMaster || operatorGo
      try {
        const home = ((await $.env.get('USERPROFILE')) ?? (await $.env.get('HOME')) ?? '').replace(/\\/g, '/')
        if (home) {
          const path = `${home}/.coord/coord-rules.log`
          let prior = ''
          try { prior = await $.fs.read(path) } catch { /* first entry */ }
          const line = `${new Date(await $.clock.now()).toISOString()} ${allow ? 'allow' : 'deny'} ` +
            `master=${isMaster} operatorGo=${operatorGo} root=${root}`
          await $.fs.write(path, [...prior.split('\n').filter(Boolean).slice(-(LOG_KEEP - 1)), line].join('\n') + '\n')
        }
      } catch { /* the log must never decide the clear */ }
      if (!allow) {
        return { deny: 'coord-rules: refusing to clear the master session. Ben\'s message this turn did not say ' +
          '"clear". Put the clear on Needs you and wait for his go in a later turn.' }
      }
      return next(e)
    }

    if (name === 'send_message' || name === 'SendMessage') {
      const sent = JSON.stringify(e)
      for (const id of [...pending.keys()]) if (sent.includes(id)) pending.delete(id)
      return next(e)
    }

    if (name === 'coord_create_task') {
      const ran = await next(e)
      if (ran.deny !== undefined || ran.isError) return ran
      const id = TASK_ID.exec(String(ran.text ?? ''))?.[1]
      if (!id) return ran
      const params = ((e as Record<string, unknown>).params ?? {}) as Record<string, unknown>
      const client = String(params.assigned_to ?? 'unassigned')
      pending.set(id, client)
      return { ...ran, context: [...(ran.context ?? []),
        `coord-rules: task ${id} (${client}) is only a row until its session gets ` +
        `send_message "Do task ${id}. Start with coord_get_task." -- unless this client already has one in flight.`] }
    }

    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId) return next(e)
    operatorGo = false
    if (pending.size) {
      carried = [...pending].map(([id, client]) => `${id} (${client})`)
      pending.clear()
      $.ui.toast(`coord-rules: not dispatched this turn: ${carried.join(', ')}`, { timeoutMs: 12000 })
    }
    return next(e)
  })
}
