import { createRouter, createWebHistory } from 'vue-router'
import { auth } from './state/auth'
import LoginView from './views/LoginView.vue'
import RegisterView from './views/RegisterView.vue'
import ChatView from './views/ChatView.vue'
import BuildsView from './views/BuildsView.vue'
import ComponentsView from './views/ComponentsView.vue'
import MemoryView from './views/MemoryView.vue'
import ProfileView from './views/ProfileView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: LoginView, meta: { guest: true } },
    { path: '/register', component: RegisterView, meta: { guest: true } },
    { path: '/', component: ChatView, meta: { auth: true } },
    { path: '/builds', component: BuildsView, meta: { auth: true } },
    { path: '/components', component: ComponentsView, meta: { auth: true } },
    { path: '/memory', component: MemoryView, meta: { auth: true } },
    { path: '/profile', component: ProfileView, meta: { auth: true } }
  ]
})

router.beforeEach((to) => {
  if (to.meta.auth && !auth.user) return '/login'
  if (to.meta.guest && auth.user) return '/'
})

export default router
