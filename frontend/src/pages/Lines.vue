<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'

const rows = ref([])
const tills = ref([])
const preview = ref(null)
const day = ref('')
const form = ref({ till_code: 'TILL-A', amount: 10, pay_method: '现金', note: '' })
const msg = ref('')
const err = ref('')

const tillState = computed(() => {
  const m = {}
  if (preview.value) {
    for (const t of preview.value.till_summary) m[t.till_code] = t
  }
  return m
})
const daySealed = computed(() => preview.value?.sealed === true)
const selectedLocked = computed(() => daySealed.value || tillState.value[form.value.till_code]?.till_sealed === true)

async function load() {
  err.value = ''
  const d = await api.getDay()
  day.value = d.business_day
  tills.value = await api.tills()
  rows.value = await api.lines(day.value)
  preview.value = await api.closePreview(day.value)
}

async function add() {
  msg.value = ''; err.value = ''
  try {
    await api.createLine({ ...form.value, business_day: day.value })
    msg.value = '已登记流水'
    await load()
  } catch (e) { err.value = e.message }
}

onMounted(load)
</script>

<template>
  <h2>收银流水</h2>
  <p class="msg">
    营业日 {{ day }}
    <span v-if="daySealed" class="tag ok">已日封 · 全日锁定</span>
  </p>
  <div class="row">
    <label>收银机
      <select v-model="form.till_code" :disabled="daySealed">
        <option v-for="t in tills" :key="t.till_code" :value="t.till_code">
          {{ t.title }}{{ tillState[t.till_code]?.till_sealed ? '（已机封）' : '' }}
        </option>
      </select>
    </label>
    <label>金额 <input type="number" step="0.01" v-model.number="form.amount" :disabled="selectedLocked" /></label>
    <label>支付方式
      <select v-model="form.pay_method" :disabled="selectedLocked">
        <option>现金</option><option>微信</option><option>支付宝</option>
      </select>
    </label>
    <button class="btn" :disabled="selectedLocked" @click="add">登记</button>
    <button class="btn ghost" @click="load">刷新</button>
  </div>
  <p v-if="selectedLocked" class="msg err">
    {{ daySealed ? '该营业日已整店日封，全部机具流水锁定' : `${form.till_code} 已机封，该机当日流水锁定（其他机仍可登记）` }}
  </p>
  <p v-if="msg" class="msg">{{ msg }}</p>
  <p v-if="err" class="msg err">{{ err }}</p>
  <div class="panel">
    <table>
      <thead><tr><th>单号</th><th>机</th><th>时刻</th><th>金额</th><th>方式</th><th>锁定状态</th></tr></thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ r.line_no }}</td><td>{{ r.till_code }}</td><td>{{ r.occurred_at }}</td>
          <td>{{ r.amount.toFixed(2) }}</td><td>{{ r.pay_method }}</td>
          <td>
            <span v-if="daySealed" class="tag ok">日锁</span>
            <span v-else-if="tillState[r.till_code]?.till_sealed" class="tag warn">机锁</span>
            <span v-else class="tag">可写</span>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
