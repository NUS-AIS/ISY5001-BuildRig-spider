<script setup>
import { Layers3, Trash2 } from '@lucide/vue'
import AppShell from '../components/AppShell.vue'
import BuildCard from '../components/BuildCard.vue'
import { workspace } from '../state/workspace'
</script>
<template><AppShell><div class="content-page"><div class="page-heading"><div><span class="eyebrow">YOUR WORKSPACE</span><h1>Saved builds</h1><p>Return to shortlisted options and compare their price and validation state.</p></div><span class="count-badge">{{workspace.savedBuilds.length}} saved</span></div><div v-if="workspace.savedBuilds.length" class="build-grid"><div v-for="item in workspace.savedBuilds" :key="item.option_id" class="saved-card-wrap"><BuildCard :option="item" compact/><button class="danger-link" @click="workspace.removeBuild(item.option_id)"><Trash2 :size="16"/>Remove</button></div></div><div v-else class="empty-state"><Layers3 :size="46"/><h2>No saved builds yet</h2><p>Generate a recommendation and save an option to see it here.</p><RouterLink class="primary-button fit" to="/">Ask the advisor</RouterLink></div></div></AppShell></template>
