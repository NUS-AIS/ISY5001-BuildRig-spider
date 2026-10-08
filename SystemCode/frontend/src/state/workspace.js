import { reactive } from 'vue'

const KEY = 'buildrig:workspace'
const initial = JSON.parse(localStorage.getItem(KEY) || '{}')

export const workspace = reactive({
  sessionId: initial.sessionId || null,
  requirementsVersion: initial.requirementsVersion || 0,
  savedBuilds: initial.savedBuilds || [],
  orchestrationMode: initial.orchestrationMode || 'dag',
  setSession(id, version = 0) { this.sessionId = id; this.requirementsVersion = version; this.save() },
  setVersion(version) { this.requirementsVersion = version; this.save() },
  saveBuild(option) {
    if (!this.savedBuilds.some((item) => item.option_id === option.option_id)) this.savedBuilds.unshift({ ...option, savedAt: new Date().toISOString() })
    this.save()
  },
  removeBuild(id) { this.savedBuilds = this.savedBuilds.filter((item) => item.option_id !== id); this.save() },
  setOrchestrationMode(mode) { this.orchestrationMode = mode; this.save() },
  save() { localStorage.setItem(KEY, JSON.stringify({ sessionId: this.sessionId, requirementsVersion: this.requirementsVersion, savedBuilds: this.savedBuilds, orchestrationMode: this.orchestrationMode })) }
})
