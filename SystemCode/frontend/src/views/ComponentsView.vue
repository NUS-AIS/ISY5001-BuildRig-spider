<script setup>
import { onMounted, ref, watch } from 'vue'
import { Search, SlidersHorizontal, ExternalLink, PackageSearch } from '@lucide/vue'
import AppShell from '../components/AppShell.vue'
import { api } from '../services/api'
const items=ref([]), total=ref(0), loading=ref(true), query=ref(''), category=ref(''), maxPrice=ref('')
const categories=['cpu','gpu','motherboard','ram','ssd','psu','case','cooler','laptop']
let timer
async function load(){loading.value=true;try{const p=new URLSearchParams({limit:'60',in_stock:'true'});if(query.value)p.set('q',query.value);if(category.value)p.set('category',category.value);if(maxPrice.value)p.set('max_price',maxPrice.value);const data=await api.prices(p.toString());items.value=data.items;total.value=data.total}finally{loading.value=false}}
watch([query,category,maxPrice],()=>{clearTimeout(timer);timer=setTimeout(load,250)});onMounted(load)
const money=v=>new Intl.NumberFormat('en-SG',{style:'currency',currency:'SGD',maximumFractionDigits:0}).format(v)
const sourceUrl=v=>/^https?:\/\//i.test(v||'')?v:`https://${v}`
</script>
<template><AppShell><div class="content-page"><div class="page-heading"><div><span class="eyebrow">SINGAPORE CATALOGUE</span><h1>Explore components</h1><p>Browse collected offers with source and collection context.</p></div><span class="count-badge">{{total.toLocaleString()}} matches</span></div><div class="catalog-toolbar"><label class="search-field"><Search :size="18"/><input v-model="query" placeholder="Search product name…"/></label><label><SlidersHorizontal :size="17"/><select v-model="category"><option value="">All categories</option><option v-for="cat in categories" :key="cat">{{cat}}</option></select></label><label class="price-filter">S$<input v-model="maxPrice" type="number" min="1" placeholder="Max price"/></label></div><div v-if="loading" class="catalog-grid"><div v-for="n in 8" :key="n" class="component-card skeleton"></div></div><div v-else-if="items.length" class="catalog-grid"><article v-for="item in items" :key="item.id" class="component-card"><div class="component-top"><span class="category-chip">{{item.category}}</span><span class="stock-dot">In stock</span></div><div class="component-visual"><span>{{item.brand?.slice(0,2)||item.category.slice(0,2)}}</span></div><h2>{{item.name}}</h2><p>{{item.store}}</p><div class="component-bottom"><strong>{{money(item.price)}}</strong><a :href="sourceUrl(item.source_url)" target="_blank" rel="noreferrer" aria-label="Open source"><ExternalLink :size="17"/></a></div></article></div><div v-else class="empty-state"><PackageSearch :size="46"/><h2>No matching offers</h2><p>Try another keyword, category or price ceiling.</p></div></div></AppShell></template>
