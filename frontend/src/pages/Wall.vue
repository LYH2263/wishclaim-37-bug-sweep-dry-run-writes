<template>
  <div class="wall">
    <h1 class="serif">愿望墙</h1>
    <p class="tag">无顶栏 · 瀑布流 · 点卡片认领</p>
    <div class="masonry">
      <article v-for="w in rows" :key="w.id" class="card" @click="$router.push('/wishes/'+w.id)">
        <h3>{{ w.title || '（无标题）' }}</h3>
        <p>{{ w.note }}</p>
        <p class="tag">{{ w.status }} · {{ w.data_quality }}</p>
        <p class="badges">
          <span class="badge" :class="w.claimable ? 'yes' : 'no'">
            {{ w.claimable ? '可认领' : '锁定中' }}
          </span>
          <span v-if="w.released_batch_id" class="badge batch"
                :title="'由扫尾批次释放：' + w.released_batch_id">
            TTL释放 {{ shortBatch(w.released_batch_id) }}
          </span>
        </p>
      </article>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const rows = ref([])
function shortBatch(b) { return b ? b.replace('sweep-', '') : '' }
onMounted(async () => { rows.value = await api('/wishes') })
</script>
