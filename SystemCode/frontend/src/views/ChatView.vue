<script setup>
import { computed, onMounted, onUnmounted, ref, nextTick } from 'vue'
import { Bot, Send, Sparkles, CircleAlert, Clock, Cpu, Database, Network, BrainCircuit } from '@lucide/vue'
import AppShell from '../components/AppShell.vue'
import BuildCard from '../components/BuildCard.vue'
import { api } from '../services/api'
import { workspace } from '../state/workspace'

const GREETING = 'Hi! Tell me how you plan to use your computer, your maximum budget in SGD, and whether you prefer a desktop or laptop.'
const FINISHED = ['completed', 'failed', 'cancelled']
const POLL_LIMIT_MS = 650000
const messages = ref([])
const input = ref(''); const busy = ref(false); const canGenerate = ref(false); const result = ref(null); const error = ref(''); const stage = ref('Ready'); const scroll = ref(null)
// A run the page stopped polling while it was still in progress: { id, since }.
const waiting = ref(null); const elapsed = ref(0)
let clock = null
const say = (role, text, extra = {}) => messages.value.push({ role, text, at: Date.now(), ...extra })
const clockTime = (at) => new Date(at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
const sentence = (text) => text.charAt(0).toUpperCase() + text.slice(1)
const toBottom = async () => { await nextTick(); scroll.value?.scrollTo({ top: scroll.value.scrollHeight, behavior: 'smooth' }) }
function startClock(since) { stopClock(); const tick = () => { elapsed.value = Math.max(0, Math.round((Date.now() - since) / 1000)) }; tick(); clock = setInterval(tick, 1000) }
function stopClock() { clearInterval(clock); clock = null; elapsed.value = 0 }
const orchestrationMode = ref(workspace.orchestrationMode)
const examples = ['A desktop under S$2,000 for SolidWorks and MATLAB', 'A light laptop under S$1,800 for university', 'A quiet gaming PC with 32GB RAM']

async function ensureSession() {
  if (workspace.sessionId) { try { const current=await api.getSession(workspace.sessionId); workspace.setVersion(current.requirements_version); return } catch { workspace.setSession(null,0) } }
  const session = await api.createSession(); workspace.setSession(session.id, 0)
}
async function send(text = input.value) {
  if (!text.trim() || busy.value) return
  error.value=''; say('user', text); input.value=''; busy.value=true; stage.value='Understanding your request'
  try {
    await ensureSession()
    const previous = workspace.requirementsVersion
    const parsed = await api.sendMessage(workspace.sessionId, workspace.requirementsVersion, text)
    // A message that changed nothing must not make the shown result look out of date.
    if (parsed.requirements_changed === false && workspace.lastRunVersion === previous) workspace.setLastRun(workspace.lastRunId, parsed.requirements_version)
    workspace.setVersion(parsed.requirements_version); canGenerate.value=parsed.can_generate
    say('assistant', parsed.assistant_message || (parsed.questions?.length ? parsed.questions.map(q=>q.text).join(' ') : 'Your requirements are ready. I can now prepare an evidence-backed recommendation.'), {options:parsed.reply_options||[],source:parsed.generation_source})
  } catch(e){ error.value=e.message; say('assistant', 'I could not save that request. Please check the backend connection and try again.') }
  finally { busy.value=false; stage.value='Ready'; await toBottom() }
}
// Bring back the conversation, the last result and any run still in progress after a reload.
async function restore() {
  await ensureSession()
  messages.value = []
  say('assistant', GREETING)
  let saved
  try { saved = await api.transcript(workspace.sessionId) } catch { return }
  for (const m of saved.messages) messages.value.push({ role: m.role, text: m.text, at: Date.parse(m.created_at) })
  workspace.setVersion(saved.requirements_version); canGenerate.value = saved.can_generate
  const last = messages.value.at(-1)
  if (saved.reply_options.length && last.role === 'assistant') last.options = saved.reply_options
  if (saved.latest_run) {
    try { result.value = await api.getResult(saved.latest_run.id); workspace.setLastRun(saved.latest_run.id, saved.latest_run.requirements_version) } catch { /* the result panel stays empty */ }
  }
  await toBottom()
  if (saved.active_run) follow(saved.active_run.id, Date.parse(saved.active_run.created_at))
}
// Poll one run to its end and show the outcome. Used for a new run, a run found after a reload and "Keep waiting".
async function follow(runId, since = Date.now()) {
  busy.value = true; error.value = ''; waiting.value = null; startClock(since)
  toBottom()
  try {
    const deadline = Date.now() + POLL_LIMIT_MS
    let state = await api.getRun(runId)
    while (!FINISHED.includes(state.status) && Date.now() < deadline) {
      if (state.stage) stage.value = sentence(state.stage.replaceAll('_', ' '))
      await new Promise(r => setTimeout(r, 1000)); state = await api.getRun(runId)
    }
    if (!FINISHED.includes(state.status)) { waiting.value = { id: runId, since }; stage.value = 'Still working'; return }
    if (state.status === 'failed') throw new Error(state.error?.message || 'Recommendation run failed')
    if (state.status === 'cancelled') throw new Error('The run was cancelled.')
    result.value = await api.getResult(runId); workspace.setLastRun(runId, state.requirements_version)
    stage.value = result.value.outcome === 'recommendations_available' ? 'Recommendation ready' : 'No feasible option found'
    say('assistant', result.value.assistant_message || (result.value.options?.length ? `I found ${result.value.options.length} option${result.value.options.length>1?'s':''}. Known checks and unresolved compatibility items are shown on the right.` : 'I could not find a configuration that satisfies every current requirement. Try adjusting the budget or requirements.'))
  } catch (e) { error.value = e.message; stage.value = 'Run failed' }
  finally { busy.value = false; stopClock(); await toBottom() }
}
async function generate(update = false) {
  if (busy.value) return
  busy.value=true; error.value=''; stage.value= update ? 'Updating only the parts your change affects' : 'Planning with specialist agents'
  try {
    workspace.setOrchestrationMode(orchestrationMode.value)
    const base = update ? workspace.lastRunId : null
    // The API returns the run already in progress for this session, if there is one, instead of starting another.
    const run = await api.createRun(workspace.sessionId,workspace.requirementsVersion,orchestrationMode.value,base)
    await follow(run.id, Date.parse(run.created_at) || Date.now())
  } catch(e){ error.value=e.message; stage.value='Run failed'; busy.value=false }
}
function save(option){workspace.saveBuild(option);stage.value='Saved to Builds'}
function lock(item){ send(`I want to keep the ${item.name}.`) }
const needsUpdate = computed(() => canGenerate.value && result.value && workspace.lastRunVersion !== workspace.requirementsVersion)
const groups = computed(() => {
  const options = result.value?.options || []
  const kinds = [...new Set(options.map(o => o.device_type))]
  return kinds.map(kind => ({ kind, options: options.filter(o => o.device_type === kind) }))
})
onMounted(restore)
onUnmounted(stopClock)
</script>

<template><AppShell><div class="chat-page">
  <section class="chat-panel panel">
    <header class="panel-title"><div class="title-icon"><Bot :size="23"/></div><div><h1>BuildRig Assistant</h1><p>Describe the outcome you need — the agents will handle the technical detail.</p></div><span class="live-dot">Online</span></header>
    <div ref="scroll" class="conversation">
      <div v-if="messages.length===1" class="starter"><span class="eyebrow">TRY AN EXAMPLE</span><button v-for="example in examples" :key="example" @click="send(example)">{{ example }}</button></div>
      <div v-for="(message,index) in messages" :key="index" :class="['message',message.role]"><span class="message-avatar">{{message.role==='assistant'?'BR':'You'}}</span><div><p>{{message.text}}</p><div v-if="message.options?.length" class="reply-options"><button v-for="option in message.options" :key="option.value" :disabled="busy" @click="send(option.value)">{{option.label}}</button></div><small><span v-if="message.source==='llm'" class="ai-source">Generated by qwen3:8b</span>{{clockTime(message.at)}}</small></div></div>
      <div v-if="busy" class="message assistant"><span class="message-avatar">BR</span><div class="typing"><i></i><i></i><i></i><span>{{stage}}<template v-if="elapsed"> · {{elapsed}}s</template></span></div></div>
    </div>
    <div v-if="error" class="inline-error"><CircleAlert :size="17"/>{{error}}</div>
    <div v-if="waiting && !busy" class="inline-notice"><Clock :size="17"/>This run is taking longer than usual and is still in progress.<button @click="follow(waiting.id, waiting.since)">Keep waiting</button></div>
    <div class="composer-wrap">
      <div v-if="canGenerate" class="orchestration-choice" aria-label="Agent orchestration mode">
        <button :class="{active:orchestrationMode==='dag'}" :disabled="busy" @click="orchestrationMode='dag'">DAG workflow</button>
        <button :class="{active:orchestrationMode==='pi'}" :disabled="busy" @click="orchestrationMode='pi'">Pi runtime</button>
      </div>
      <button v-if="canGenerate && !result" class="generate-button" :disabled="busy" @click="generate()"><Sparkles :size="18"/>Generate recommendation</button>
      <button v-if="needsUpdate" class="generate-button" :disabled="busy" @click="generate(true)"><Sparkles :size="18"/>Update recommendation (keep unaffected parts)</button>
      <div class="composer"><textarea v-model="input" rows="1" placeholder="Describe your needs, budget, or ask a follow-up…" @keydown.enter.exact.prevent="send()"></textarea><button :disabled="busy||!input.trim()" @click="send()"><Send :size="19"/></button></div>
      <small>Example: “A desktop for 3D modelling under S$2,000”</small>
    </div>
  </section>
  <section class="result-panel panel">
    <div v-if="result" class="run-meta">
      <span>Mode: <b>{{ result.orchestration_mode === 'pi' ? 'Pi runtime' : 'DAG workflow' }}</b></span>
      <span v-if="result.fallback" class="fallback-note">DAG revision budget exhausted — handed over to the Pi runtime</span>
      <span v-if="result.follow_up">{{ result.follow_up.reused ? 'Updated from the previous run' : 'Planned again: ' + result.follow_up.reason }}</span>
    </div>
    <div v-if="result?.limitations?.length" class="limitations"><b>Limitations</b><ul><li v-for="note in result.limitations" :key="note">{{ note }}</li></ul></div>
    <div v-if="result && !result.options?.length" class="no-option"><b>No configuration satisfies every hard constraint.</b><p>{{ result.assistant_message }}</p>
      <ul><li v-for="o in result.rejected_options || []" :key="o.option_id"><span v-for="c in o.failed_checks" :key="c.code">{{ c.reason }} </span></li></ul></div>
    <div v-if="result?.options?.length" class="result-content"><div :class="['option-groups', { compare: groups.length > 1 }]"><div v-for="group in groups" :key="group.kind" class="option-group"><h3 v-if="groups.length > 1" class="group-title">{{ group.kind === 'desktop' ? 'Desktop options' : 'Laptop options' }}</h3><BuildCard v-for="option in group.options" :key="option.option_id" :option="option" :busy="busy" @save="save" @lock="lock"/></div></div><div class="retrieval-summary"><span><Database :size="16"/>BM25</span><span><BrainCircuit :size="16"/>Vector</span><span><Network :size="16"/>Graph</span><small>Evidence routes are fused and reviewed before output.</small></div></div>
    <div v-else-if="!result" class="empty-result"><div class="empty-orbit"><Cpu :size="42" :stroke-width="1.7"/><span></span></div><span class="eyebrow">RECOMMENDATION WORKSPACE</span><h2>Your tailored option will appear here</h2><p>Share your use case, budget and must-have requirements. BuildRig will coordinate specialist agents, retrieve evidence and expose every unresolved check.</p><div class="empty-steps"><div><b>1</b>Understand</div><div><b>2</b>Retrieve</div><div><b>3</b>Validate</div></div><button v-if="canGenerate" class="primary-button fit" :disabled="busy" @click="generate()"><Sparkles :size="18"/>Start agent workflow</button></div>
  </section>
</div></AppShell></template>
