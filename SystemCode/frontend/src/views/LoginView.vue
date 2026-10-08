<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight, Eye, EyeOff } from '@lucide/vue'
import AuthLayout from '../components/AuthLayout.vue'
import { auth } from '../state/auth'
const router = useRouter(); const form = reactive({ email: '', password: '' }); const error = ref(''); const show = ref(false)
function submit() { try { auth.login(form); router.push('/') } catch (e) { error.value = e.message } }
</script>
<template><AuthLayout><div class="auth-heading"><span class="eyebrow">WELCOME BACK</span><h2>Sign in to BuildRig</h2><p>Continue your recommendation workspace.</p></div><form @submit.prevent="submit" class="auth-form"><label>Email address<input v-model.trim="form.email" type="email" placeholder="you@example.com" required /></label><label>Password<div class="password-field"><input v-model="form.password" :type="show?'text':'password'" placeholder="Enter your password" required /><button type="button" @click="show=!show"><EyeOff v-if="show" :size="18"/><Eye v-else :size="18"/></button></div></label><div class="form-row"><label class="check-label"><input type="checkbox" /> Remember me</label><a href="#" @click.prevent>Forgot password?</a></div><p v-if="error" class="form-error">{{ error }}</p><button class="primary-button" type="submit">Sign in <ArrowRight :size="18" /></button></form><p class="auth-switch">New to BuildRig? <RouterLink to="/register">Create an account</RouterLink></p><p class="demo-note">Course prototype: account credentials are stored in this browser only.</p></AuthLayout></template>
