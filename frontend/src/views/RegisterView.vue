<script setup>
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight } from '@lucide/vue'
import AuthLayout from '../components/AuthLayout.vue'
import { auth } from '../state/auth'
const router=useRouter(); const form=reactive({name:'',email:'',password:'',agree:false}); const error=ref('')
function submit(){ if(form.password.length<8){error.value='Use at least 8 characters.';return} try{auth.register(form);router.push('/')}catch(e){error.value=e.message} }
</script>
<template><AuthLayout><div class="auth-heading"><span class="eyebrow">CREATE YOUR WORKSPACE</span><h2>Start building smarter</h2><p>Save recommendations and refine them over time.</p></div><form @submit.prevent="submit" class="auth-form"><label>Full name<input v-model.trim="form.name" placeholder="Your name" required /></label><label>Email address<input v-model.trim="form.email" type="email" placeholder="you@example.com" required /></label><label>Password<input v-model="form.password" type="password" placeholder="At least 8 characters" required /></label><label class="check-label terms"><input v-model="form.agree" type="checkbox" required /> I agree to the Terms and Privacy Policy.</label><p v-if="error" class="form-error">{{ error }}</p><button class="primary-button" type="submit">Create account <ArrowRight :size="18" /></button></form><p class="auth-switch">Already have an account? <RouterLink to="/login">Sign in</RouterLink></p><p class="demo-note">Course prototype: account credentials are stored in this browser only.</p></AuthLayout></template>
