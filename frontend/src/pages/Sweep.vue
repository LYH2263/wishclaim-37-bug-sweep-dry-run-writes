<template>
  <div class="wall">
    <h1 class="serif">过期扫尾</h1>
    <p class="tag">干跑只读：列出将释放的愿望与认领人；提交才真正释放并追加台账。</p>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="ghost" @click="dryRun">干跑预览</button>
      <button @click="commit" :disabled="!candidates.length">提交扫尾</button>
    </div>
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="lastBatch" class="ok">已提交批次 <code>{{ lastBatch }}</code>，本次记账 {{ lastCount }} 条</p>

    <h3 class="serif">候选（{{ candidates.length }}）</h3>
    <p v-if="!candidates.length" class="tag">没有待释放的过期锁。</p>
    <div class="masonry" style="column-count:1">
      <article v-for="x in candidates" :key="x.wish_id" class="card"
               @click="$router.push('/wishes/'+x.wish_id)">
        <h3>愿望 #{{ x.wish_id }}</h3>
        <p class="tag">认领人 {{ x.claimer || '—' }}</p>
        <p class="tag">过期于 {{ x.expires_at }}</p>
      </article>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const candidates = ref([])
const lastBatch = ref('')
const lastCount = ref(0)
const err = ref('')
async function dryRun() {
  err.value = ''
  try { const r = await api('/sweep/dry-run'); candidates.value = r.candidates }
  catch (e) { err.value = e.message }
}
async function commit() {
  err.value = ''
  try {
    const r = await api('/sweep/commit', { method: 'POST', body: '{}' })
    lastBatch.value = r.batch_id || ''
    lastCount.value = r.ledgered
    await dryRun()
  } catch (e) { err.value = e.message }
}
onMounted(dryRun)
</script>
