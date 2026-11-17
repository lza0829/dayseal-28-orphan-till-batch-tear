<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const day = ref('')
const strategy = ref('store')
const locked = ref(false)
const msg = ref('')
const err = ref('')

onMounted(async () => {
  const d = await api.getDay()
  day.value = d.business_day
  const s = await api.getStrategy()
  strategy.value = s.close_strategy
  locked.value = s.has_sealed_batch
})

async function saveDay() {
  msg.value = ''; err.value = ''
  try {
    await api.setDay(day.value)
    msg.value = '已更新营业日'
  } catch (e) { err.value = e.message }
}

async function saveStrategy() {
  msg.value = ''; err.value = ''
  try {
    const s = await api.setStrategy(strategy.value)
    locked.value = s.has_sealed_batch
    msg.value = `封账范围策略已切换为：${strategy.value === 'till' ? '分机先封' : '整店日封'}`
  } catch (e) { err.value = e.message }
}
</script>

<template>
  <h2>设置</h2>
  <div class="row">
    <label>营业日 <input v-model="day" placeholder="YYYY-MM-DD" /></label>
    <button class="btn" @click="saveDay">保存</button>
  </div>

  <div class="panel">
    <h2>封账范围（全局互斥，只贯彻一种）</h2>
    <div class="row">
      <label>策略
        <select v-model="strategy" :disabled="locked">
          <option value="store">整店日封：一次封账覆盖全日全部机具，实盘为整店现金</option>
          <option value="till">分机先封：每台机独立机封，日封校验全部机封后收口</option>
        </select>
      </label>
      <button class="btn" :disabled="locked" @click="saveStrategy">保存策略</button>
    </div>
    <p v-if="locked" class="msg err">
      已存在 sealed 封账批次，状态机已锁定，不可切换策略（防止整店日封与机封批次混用）。
    </p>
    <p v-else class="msg">仅在尚无任何封账批次时允许切换；切换后全店统一按该策略执行。</p>
  </div>

  <p v-if="msg" class="msg">{{ msg }}</p>
  <p v-if="err" class="msg err">{{ err }}</p>
</template>
