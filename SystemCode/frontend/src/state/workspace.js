import { reactive } from 'vue'

// One workspace per signed-in person: saved builds and the current conversation are not shared between accounts.
const LEGACY_KEY = 'buildrig:workspace'
const keyFor = (userId) => `${LEGACY_KEY}:${userId || 'guest'}`

function stored(userId) {
  try {
    const own = localStorage.getItem(keyFor(userId))
    if (own) return JSON.parse(own)
    // Earlier versions kept a single workspace for the whole browser; the first person to sign in keeps it.
    const legacy = localStorage.getItem(LEGACY_KEY)
    if (legacy && userId) { localStorage.removeItem(LEGACY_KEY); return JSON.parse(legacy) }
  } catch { /* unreadable storage starts empty */ }
  return {}
}
const fields = (saved) => ({
  sessionId: saved.sessionId || null,
  requirementsVersion: saved.requirementsVersion || 0,
  savedBuilds: saved.savedBuilds || [],
  orchestrationMode: saved.orchestrationMode || 'dag',
  lastRunId: saved.lastRunId || null,
  lastRunVersion: saved.lastRunVersion ?? null
})
let currentUser = null
try { currentUser = JSON.parse(localStorage.getItem('buildrig:user') || 'null')?.id || null } catch { /* signed out */ }

export const workspace = reactive({
  ...fields(stored(currentUser)),
  load(userId) { currentUser = userId || null; Object.assign(this, fields(stored(currentUser))) },
  setSession(id, version = 0) { this.sessionId = id; this.requirementsVersion = version; this.lastRunId = null; this.lastRunVersion = null; this.save() },
  setLastRun(id, version) { this.lastRunId = id; this.lastRunVersion = version; this.save() },
  setVersion(version) { this.requirementsVersion = version; this.save() },
  isSaved(option) { return this.savedBuilds.some((item) => item.option_id === option.option_id) },
  saveBuild(option) {
    if (!this.isSaved(option)) this.savedBuilds.unshift({ ...option, savedAt: new Date().toISOString() })
    this.save()
  },
  removeBuild(id) { this.savedBuilds = this.savedBuilds.filter((item) => item.option_id !== id); this.save() },
  setOrchestrationMode(mode) { this.orchestrationMode = mode; this.save() },
  save() { localStorage.setItem(keyFor(currentUser), JSON.stringify({ sessionId: this.sessionId, requirementsVersion: this.requirementsVersion, savedBuilds: this.savedBuilds, orchestrationMode: this.orchestrationMode, lastRunId: this.lastRunId, lastRunVersion: this.lastRunVersion })) }
})
