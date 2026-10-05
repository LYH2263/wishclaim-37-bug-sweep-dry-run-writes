<template>
  <div class="wall">
    <h1 class="serif">释放台账</h1>
    <p class="tag">只记录扫尾提交释放的过期锁；手动释放不记账，重复提交不双增。</p>
    <p v-if="!batches.length" class="tag">暂无台账。</p>
    <section v-for="b in batches" :key="b.batch_id" class="batch">
      <header class="batch-head">
        <code>{{ b.batch_id }}</code>
        <span class="tag">{{ b.released_at }} · {{ b.entries.length }} 条</span>
      </header>
      <div class="masonry" style="column-count:1">
        <article v-for="e in b.entries" :key="e.wish_id+'|'+e.claimed_at" class="card"
                 @click="$router.push('/wishes/'+e.wish_id)">
          <h3>愿望 #{{ e.wish_id }}</h3>
          <p class="tag">原认领人 {{ e.claimer || '—' }} · 认领于 {{ e.claimed_at }}</p>
        </article>
      </div>
    </section>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const batches = ref([])
onMounted(async () => { batches.value = await api('/ledger') })
</script>
