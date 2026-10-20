export type Parked = { id: string; title: string; client: string; parks: number; prompt: string }

// The question master put to Ben with coord_present_question. prompts[i] is
// what option i's button sends, built by the board from the stored row.
export type Question = {
  id: number
  ref: string
  question: string
  options: string[]
  prompts: string[]
  recommended: number | null
}

declare module 'claude-code' {
  interface PluginState {
    'master-console': { parked: Parked[]; actions: Record<string, string>; question: Question | null }
  }
}
