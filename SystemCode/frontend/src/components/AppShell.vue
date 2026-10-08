<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { MessageCircle, Layers3, Cpu, BrainCircuit, Search, UserRound, LogOut, Menu, X } from '@lucide/vue'
import BrandLogo from './BrandLogo.vue'
import { auth } from '../state/auth'

const router = useRouter()
const open = ref(false)
const links = [
  { to: '/', label: 'Advisor', icon: MessageCircle },
  { to: '/builds', label: 'Builds', icon: Layers3 },
  { to: '/components', label: 'Components', icon: Cpu },
  { to: '/memory', label: 'Memory', icon: BrainCircuit }
]
function logout() { auth.logout(); router.push('/login') }
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <BrandLogo />
      <nav :class="['main-nav', { open }]">
        <RouterLink v-for="item in links" :key="item.to" :to="item.to" @click="open=false">
          <component :is="item.icon" :size="17" />{{ item.label }}
        </RouterLink>
      </nav>
      <div class="top-actions">
        <RouterLink class="icon-button search-button" to="/components" aria-label="Search components"><Search :size="19" /></RouterLink>
        <RouterLink class="profile-chip" to="/profile"><span class="avatar">{{ auth.user?.name?.slice(0,1).toUpperCase() }}</span><span>{{ auth.user?.name }}</span><UserRound :size="15" /></RouterLink>
        <button class="icon-button desktop-only" @click="logout" aria-label="Log out"><LogOut :size="18" /></button>
        <button class="icon-button menu-button" @click="open=!open" aria-label="Toggle menu"><X v-if="open" /><Menu v-else /></button>
      </div>
    </header>
    <main><slot /></main>
  </div>
</template>
