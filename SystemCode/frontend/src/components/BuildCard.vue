<script setup>
import { computed } from 'vue'
import { CheckCircle2, AlertTriangle, Bookmark, ExternalLink } from '@lucide/vue'
const props = defineProps({ option: { type: Object, required: true }, compact: Boolean })
defineEmits(['save'])
const total = computed(() => (props.option.validation?.total_minor || props.option.items.reduce((sum, item) => sum + Math.round(Number(item.price) * 100), 0)) / 100)
const status = computed(() => props.option.validation?.overall_status || 'unknown')
const money = (value) => new Intl.NumberFormat('en-SG', { style: 'currency', currency: 'SGD', maximumFractionDigits: 0 }).format(value)
</script>
<template>
  <article class="build-card">
    <div class="build-card-head">
      <div><span class="eyebrow">{{ option.device_type === 'desktop' ? 'CUSTOM DESKTOP' : 'LAPTOP PICK' }}</span><h2>{{ option.title }}</h2></div>
      <span :class="['status-pill', status]"><CheckCircle2 v-if="status==='passed'" :size="16" /><AlertTriangle v-else :size="16" />{{ status === 'passed' ? 'Checks passed' : status === 'failed' ? 'Check failed' : 'Checks need review' }}</span>
    </div>
    <div class="parts-list">
      <div v-for="item in option.items" :key="item.offer_id" class="part-row">
        <span class="part-icon">{{ item.category.slice(0,2).toUpperCase() }}</span>
        <div class="part-copy"><strong>{{ item.category.replace('_',' ') }}</strong><span>{{ item.name }}</span></div>
        <a :href="item.source_url" target="_blank" rel="noreferrer" class="merchant">{{ item.merchant }} <ExternalLink :size="12" /></a>
        <strong class="part-price">{{ money(Number(item.price)) }}</strong>
      </div>
    </div>
    <div class="build-total"><span>Total price</span><strong>{{ money(total) }}</strong></div>
    <div v-if="!compact" class="build-insights">
      <h3>Why this option?</h3>
      <ol><li v-for="reason in option.reasons" :key="reason">{{ reason }}</li><li v-for="trade in option.trade_offs" :key="trade">{{ trade }}</li></ol>
      <div v-if="option.validation?.checks" class="checks">
        <span v-for="check in option.validation.checks" :key="check.code" :class="check.status">{{ check.code.replaceAll('_',' ') }} · {{ check.status }}</span>
      </div>
    </div>
    <button v-if="!compact" class="secondary-button save-button" @click="$emit('save', option)"><Bookmark :size="17" /> Save this option</button>
  </article>
</template>
