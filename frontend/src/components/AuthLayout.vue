<script setup>
import { ShieldCheck, DatabaseZap, Sparkles } from '@lucide/vue'
import BrandLogo from './BrandLogo.vue'
</script>
<template>
  <div class="auth-layout">
    <section class="auth-brand-panel">
      <BrandLogo />
      <div class="auth-pitch">
        <span class="eyebrow"><Sparkles :size="15" /> Singapore PC intelligence</span>
        <h1>Build with evidence.<br><span>Choose with confidence.</span></h1>
        <p>One intelligent workspace for desktop builds and laptop recommendations, grounded in local prices, product relationships and real user feedback.</p>
        <div class="auth-benefits">
          <div><ShieldCheck /><span><strong>Constraint checked</strong>Budget and known compatibility rules stay visible.</span></div>
          <div><DatabaseZap /><span><strong>Source backed</strong>Every recommendation connects to current evidence.</span></div>
        </div>
      </div>
      <p class="auth-foot">Designed for Singapore buyers · Prices shown in SGD</p>
    </section>
    <section class="auth-form-panel"><div class="auth-card"><slot /></div></section>
  </div>
</template>
