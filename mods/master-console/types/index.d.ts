export type Parked = { id: string; title: string; client: string; parks: number; prompt: string }

declare module 'claude-code' {
  interface PluginState {
    'master-console': { parked: Parked[]; actions: Record<string, string> }
  }
}
