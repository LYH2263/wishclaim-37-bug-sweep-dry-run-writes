<template>
  <div class="wall">
    <h1 class="serif">{{ w.title }}</h1>
    <p>{{ w.note }}</p>
    <p class="tag">状态 {{ w.status }} · 认领人 {{ w.claimer || '—' }}</p>
    <p v-if="err" class="err">{{ err }}</p>
    <input v-model="claimer" placeholder="你的名字" />
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button @click="claim">认领锁定</button>
      <button class="ghost" @click="release">释放</button>
      <button class="ghost" @click="fulfill">核销完成</button>
    </div>

    <h3 class="serif">事件</h3>
    <p v-if="!events.length" class="tag">暂无 TTL 释放事件。</p>
    <ul class="events">
      <li v-for="(e, i) in events" :key="i">
        <span class="badge batch">{{ shortBatch(e.batch_id) }}</span>
        TTL 释放 · 原认领人 {{ e.claimer || '—' }}
        <span class="tag">{{ e.at }}</span>
      </li>
    </ul>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const props = defineProps({ id: String })
const w = ref({})
const events = ref([])
const claimer = ref('访客')
const err = ref('')
function shortBatch(b) { return b ? b.replace('sweep-', '') : '' }
async function load() {
  w.value = await api('/wishes/' + props.id)
  events.value = await api('/wishes/' + props.id + '/events')
}
async function claim() {
  err.value=''; try { await api('/wishes/'+props.id+'/claim',{method:'POST',body:JSON.stringify({claimer:claimer.value})}); await load() } catch(e){ err.value=e.message }
}
async function release() {
  err.value=''; try { await api('/wishes/'+props.id+'/release',{method:'POST',body:'{}'}); await load() } catch(e){ err.value=e.message }
}
async function fulfill() {
  err.value=''; try { await api('/wishes/'+props.id+'/fulfill',{method:'POST',body:'{}'}); await load() } catch(e){ err.value=e.message }
}
onMounted(load)
</script>
