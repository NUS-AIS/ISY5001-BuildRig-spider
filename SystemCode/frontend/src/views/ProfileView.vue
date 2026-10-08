<script setup>
import { ref, onMounted } from 'vue'
import { UserRound, Server, Database, LogOut } from '@lucide/vue'
import { useRouter } from 'vue-router'
import AppShell from '../components/AppShell.vue'
import { auth } from '../state/auth'
import { api } from '../services/api'
const router=useRouter(), health=ref(null);onMounted(async()=>{try{health.value=await api.health()}catch{}})
function logout(){auth.logout();router.push('/login')}
</script>
<template><AppShell><div class="content-page narrow"><div class="page-heading"><div><span class="eyebrow">ACCOUNT</span><h1>Profile and environment</h1><p>Review your prototype account and active backend mode.</p></div></div><section class="profile-card"><span class="profile-avatar"><UserRound :size="30"/></span><div><h2>{{auth.user.name}}</h2><p>{{auth.user.email}}</p><small>{{auth.user.id}}</small></div></section><section class="settings-card"><h2>System status</h2><div class="setting-row"><Server/><span><strong>API service</strong><small>{{health?'Connected':'Unavailable'}}</small></span><b :class="health?'good':'bad'">{{health?'Online':'Offline'}}</b></div><div class="setting-row"><Database/><span><strong>Retrieval backend</strong><small>Snapshot {{health?.snapshot_id||'—'}}</small></span><b>{{health?.retrieval_backend||'Unknown'}}</b></div></section><button class="danger-button" @click="logout"><LogOut :size="17"/>Sign out</button><p class="demo-note left">Authentication is browser-local in this course prototype. Production deployment must replace it with server-side password hashing, tokens and verified identity headers.</p></div></AppShell></template>
