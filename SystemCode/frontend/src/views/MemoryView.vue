<script setup>
import { onMounted, ref } from 'vue'
import { BrainCircuit, Plus, Trash2, ShieldCheck } from '@lucide/vue'
import AppShell from '../components/AppShell.vue'
import { api } from '../services/api'
import { workspace } from '../state/workspace'
const items=ref([]), value=ref(''), kind=ref('preference'), error=ref('')
async function load(){if(!workspace.sessionId)return;try{items.value=(await api.memories(workspace.sessionId)).items}catch(e){error.value=e.message}}
async function add(){if(!value.value.trim())return;await api.addMemory(workspace.sessionId,kind.value,value.value.trim());value.value='';await load()}
async function remove(id){await api.deleteMemory(id);await load()}
onMounted(load)
</script>
<template><AppShell><div class="content-page narrow"><div class="page-heading"><div><span class="eyebrow">CONTROL YOUR CONTEXT</span><h1>Recommendation memory</h1><p>Only confirmed preferences are reused in future agent runs.</p></div></div><div class="memory-info"><ShieldCheck :size="22"/><div><strong>You stay in control</strong><p>Current request constraints always override saved preferences. Removing a memory prevents it from being used again.</p></div></div><form class="memory-form" @submit.prevent="add"><select v-model="kind"><option value="preference">Preference</option><option value="workload">Workload</option><option value="owned_device">Owned device</option></select><input v-model="value" placeholder="e.g. Prefer quiet systems"/><button class="primary-button fit"><Plus :size="17"/>Add memory</button></form><p v-if="error" class="form-error">{{error}}</p><div v-if="items.length" class="memory-list"><article v-for="item in items" :key="item.id"><span class="memory-icon"><BrainCircuit :size="19"/></span><div><small>{{item.kind.replace('_',' ')}}</small><strong>{{item.value}}</strong><span>Confirmed · Version {{item.version}}</span></div><button @click="remove(item.id)" aria-label="Delete memory"><Trash2 :size="17"/></button></article></div><div v-else class="empty-state small"><BrainCircuit :size="40"/><h2>No saved preferences</h2><p>Add only details you want the advisor to reuse.</p></div></div></AppShell></template>
