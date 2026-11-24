export type Parked = { id: string; title: string; client: string; parks: number; prompt: string }

// The question master put to Ben with coord_present_question. prompts[i] is
// what option i's button sends, built by the board from the stored row.
export type Question = {
  id: number
  ref: string
  task_id: string | null  // null: board-wide, ref is q<id>
  path: string | null     // the document behind it
  question: string
  question_labeled?: string  // question with each id's label beside it (lookup.label_question)
  ref_label?: string         // what the answer lands on: "task: <title>", "loose end: <text>"
  options: string[]
  prompts: string[]
  recommended: number | null
}

// A finished direct ask the board handed back (coord_mcp/direct.py).
export type Ready = { root_id: string; client: string | null; title: string; what: string }

declare module 'claude-code' {
  interface PluginState {
    'master-console': {
      parked: Parked[]; actions: Record<string, string>; question: Question | null; ready: Ready | null
    }
  }
}
