<script setup>
import { computed, ref } from 'vue'
import { CheckCircle2, AlertTriangle, XCircle, HelpCircle, Bookmark, ExternalLink, Lock, History, FileSearch } from '@lucide/vue'

const props = defineProps({ option: { type: Object, required: true }, compact: Boolean, busy: Boolean })
defineEmits(['save', 'lock'])

const total = computed(() => (props.option.validation?.total_minor ?? props.option.items.reduce((sum, item) => sum + Math.round(Number(item.price) * 100), 0)) / 100)
const status = computed(() => props.option.validation?.overall_status || 'unknown')
const checks = computed(() => {
  const order = { failed: 0, unknown: 1, passed: 2 }
  return [...(props.option.validation?.checks || [])].sort((a, b) => order[a.status] - order[b.status])
})
const counts = computed(() => checks.value.reduce((acc, c) => ({ ...acc, [c.status]: (acc[c.status] || 0) + 1 }), {}))
const showEvidence = ref(false)
const money = (value) => new Intl.NumberFormat('en-SG', { style: 'currency', currency: 'SGD', maximumFractionDigits: 0 }).format(value)
const date = (value) => value ? new Date(value).toLocaleDateString('en-SG', { day: 'numeric', month: 'short', year: 'numeric' }) : ''
const label = (code) => code.replaceAll('_', ' ')
const levelText = { exact_offer: 'listing', model_family_only: 'model family', brand_only: 'brand', derived_from_specs: 'derived from specs' }
</script>

<template>
  <article class="build-card">
    <div class="build-card-head">
      <div>
        <span class="eyebrow">{{ option.device_type === 'desktop' ? 'CUSTOM DESKTOP' : 'LAPTOP PICK' }}</span>
        <h2>{{ option.title || 'Recommended option' }}</h2>
      </div>
      <span :class="['status-pill', status]">
        <CheckCircle2 v-if="status==='passed'" :size="16" /><XCircle v-else-if="status==='failed'" :size="16" /><AlertTriangle v-else :size="16" />
        {{ status === 'passed' ? 'All checks passed' : status === 'failed' ? 'Check failed' : `${counts.unknown || 0} check(s) unknown` }}
      </span>
    </div>

    <div class="parts-list">
      <div v-for="item in option.items" :key="item.offer_id || item.product_id" class="part-row">
        <span class="part-icon">{{ item.category.slice(0,2).toUpperCase() }}</span>
        <div class="part-copy">
          <strong>{{ item.category.replace('_',' ') }}
            <em v-if="item.owned_by_user" class="part-badge owned">Owned</em>
            <em v-else-if="item.locked" class="part-badge locked">Locked</em>
            <em v-if="item.carried_over && !item.locked && !item.owned_by_user" class="part-badge kept">Kept</em>
          </strong>
          <span>{{ item.name }}</span>
          <small v-if="item.collected_at" class="part-meta">Price collected {{ date(item.collected_at) }}</small>
        </div>
        <a v-if="item.source_url" :href="item.source_url" target="_blank" rel="noreferrer" class="merchant">{{ item.merchant }} <ExternalLink :size="12" /></a>
        <span v-else class="merchant">—</span>
        <div class="part-actions">
          <strong class="part-price">{{ item.owned_by_user ? 'S$0' : money(Number(item.price)) }}</strong>
          <button v-if="!compact && !item.locked && !item.owned_by_user" class="lock-button" :disabled="busy" title="Keep this part in the next update" @click="$emit('lock', item)"><Lock :size="13" /></button>
        </div>
      </div>
    </div>
    <div class="build-total"><span>Total price</span><strong>{{ money(total) }}</strong></div>

    <div v-if="!compact" class="build-insights">
      <div v-if="option.follow_up" class="follow-up-note">
        Updated from your previous recommendation: kept {{ option.follow_up.kept_parts.length }} part(s), changed {{ option.follow_up.changed_parts.length }}.
      </div>

      <h3>Why this option?</h3>
      <ol><li v-for="reason in option.reasons" :key="reason">{{ reason }}</li></ol>
      <h3 v-if="option.trade_offs?.length">Trade-offs</h3>
      <ul class="trade-offs"><li v-for="trade in option.trade_offs" :key="trade">{{ trade }}</li></ul>

      <h3>Checks</h3>
      <ul class="check-list">
        <li v-for="check in checks" :key="check.code" :class="check.status">
          <CheckCircle2 v-if="check.status==='passed'" :size="15" /><XCircle v-else-if="check.status==='failed'" :size="15" /><HelpCircle v-else :size="15" />
          <span><b>{{ label(check.code) }}</b> — {{ check.reason }}</span>
        </li>
      </ul>

      <div v-if="option.revision_history?.length" class="timeline">
        <h3><History :size="15" /> How the plan was revised</h3>
        <div v-for="round in option.revision_history" :key="round.round" class="timeline-step">
          <span class="timeline-dot">{{ round.round }}</span>
          <div>
            <b>Failed: {{ (round.failed_checks || []).map(label).join(', ') || 'preference review' }}</b>
            <p v-for="change in round.changes || []" :key="change.category">
              {{ change.category }}: {{ change.from }} <span v-if="change.to">→ {{ change.to }} ({{ money(Number(change.from_price)) }} → {{ money(Number(change.to_price)) }})</span><span v-else>— {{ change.note }}</span>
            </p>
          </div>
        </div>
      </div>

      <button v-if="option.evidence?.length" class="evidence-toggle" @click="showEvidence=!showEvidence">
        <FileSearch :size="15" /> {{ showEvidence ? 'Hide' : 'Show' }} evidence ({{ option.evidence.length }})
      </button>
      <ul v-if="showEvidence" class="evidence-list">
        <li v-for="item in option.evidence" :key="item.evidence_id">
          <div class="evidence-head">
            <span class="evidence-kind">{{ item.kind }}</span>
            <span v-if="item.match_level" class="evidence-level">{{ levelText[item.match_level] || item.match_level }}</span>
            <span class="evidence-routes">{{ Object.keys(item.route_ranks || {}).join(' + ') }}</span>
            <a v-if="item.source_url" :href="item.source_url" target="_blank" rel="noreferrer">source <ExternalLink :size="11" /></a>
          </div>
          <p>{{ item.excerpt?.slice(0, 260) }}</p>
        </li>
      </ul>
    </div>
    <button v-if="!compact" class="secondary-button save-button" @click="$emit('save', option)"><Bookmark :size="17" /> Save this option</button>
  </article>
</template>
