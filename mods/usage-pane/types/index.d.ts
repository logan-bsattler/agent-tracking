export type Snapshot = {
  limits: { kind: string; percentUsed: number; resetsAt?: string }[]
  ctxTokens?: number
  ctxWindow: number
  ctxPercent?: number
  costUsd?: number
  categories: { name: string; tokens: number }[]
  at: number
}

export type Tokens = {
  input: number
  output: number
  cacheRead: number
  cacheWrite: number
  turns: number
  byModel: Record<string, number>
}

declare module 'claude-code' {
  interface PluginState {
    'usage-pane': { snap: Snapshot | null; tokens: Tokens }
  }
}
