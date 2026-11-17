<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { api } from '../api'

const preview = ref(null)
const batches = ref([])
const dayDeclared = ref(0)
const tillDeclared = reactive({})
const waive = reactive({})
const msg = ref('')
const err = ref('')
const busy = ref('')

const isTill = computed(() => preview.value?.strategy === 'till')
// 零流水机必须逐台显式收口；未勾齐时按钮即禁用（服务端提交时还会再强校验一次）
const waiveComplete = computed(() => {
  if (!preview.value || !isTill.value) return true
  return preview.value.inactive_tills.every((c) => waive[c])
})
const daySealDisabled = computed(() =>
  !preview.value || preview.value.sealed || busy.value === 'day'
  || !preview.value.can_day_seal || !waiveComplete.value,
)

async function load() {
  msg.value = ''; err.value = ''
  preview.value = await api.closePreview()
  dayDeclared.value = preview.value.system_total
  for (const t of preview.value.till_summary) {
    if (tillDeclared[t.till_code] == null) tillDeclared[t.till_code] = t.system_total
  }
  for (const code of preview.value.inactive_tills) waive[code] = waive[code] ?? preview.value.waived_tills.includes(code)
  batches.value = await api.batches()
}

async function sealTill(code) {
  msg.value = ''; err.value = ''; busy.value = code
  try {
    const r = await api.sealTill({
      business_day: preview.value.business_day,
      till_code: code,
      declared_cash: tillDeclared[code],
    })
    msg.value = `${code} 已机封：系统 ${r.system_total.toFixed(2)} / 实盘 ${r.declared_cash.toFixed(2)} / 机级长短款 ${r.variance.toFixed(2)}`
    await load()
  } catch (e) { err.value = e.message } finally { busy.value = '' }
}

async function sealDay() {
  msg.value = ''; err.value = ''; busy.value = 'day'
  try {
    const body = {
      business_day: preview.value.business_day,
      declared_cash: dayDeclared.value,
    }
    if (isTill.value) body.waived_tills = Object.keys(waive).filter((c) => waive[c])
    const r = await api.seal(body)
    const scope = isTill.value
      ? `全店日封完成（机封 ${r.sealed_till_count} 台${r.waived_tills?.length ? `，收口 ${r.waived_tills.join(',')}` : ''}）`
      : '整店日封完成'
    msg.value = `${scope}：系统 ${r.system_total.toFixed(2)} / 实盘 ${r.declared_cash.toFixed(2)} / 日级长短款 ${r.variance.toFixed(2)}`
    await load()
  } catch (e) { err.value = e.message } finally { busy.value = '' }
}

// 作废血缘收敛：日批作废级联释放其引用机批；机批作废驱动父日批一并作废
async function voidBatch(b) {
  const hint = b.scope === 'day'
    ? '其引用的机批将一并作废释放，全日可重写'
    : '引用它的日批将连同快照与合计一并作废'
  if (!confirm(`确认作废批次 ${b.batch_no}（${b.scope === 'day' ? '日级' : `机级·${b.till_code}`}）？${hint}。`)) return
  msg.value = ''; err.value = ''; busy.value = `void-${b.id}`
  try {
    const r = await api.voidBatch(b.id)
    const extra = r.scope === 'day'
      ? (r.cascaded_till_codes?.length ? `，级联释放机批：${r.cascaded_till_codes.join(', ')}` : '')
      : (r.parent_day_batch_id ? '，父级日批已一并作废' : '')
    msg.value = `批次 ${r.batch_no} 已作废${extra}`
    await load()
  } catch (e) { err.value = e.message } finally { busy.value = '' }
}

onMounted(load)

function tillRefHint(batch) {
  if (!batch) return []
  return batch.till_refs || batch.till_codes || []
}

/* align-28 */
function alignHint(batch) {
  if (!batch) return ''
  const v = Number(batch.variance || 0)
  const s = Number(batch.system_total || 0)
  const d = Number(batch.declared_cash || 0)
  return [s, d, v].join('/')
}
function alignRows(rows) {
  return (rows || []).map(r => ({ ...r, align: alignHint(r) }))
}

/* fence-28 */

function precheckView(p) {
  if (!p) return {}
  return { gate: p.gate_open, fp: p.fp_ok, bypass: p.bypass }
}

</script>

<template>
  <h2>日结封账</h2>
  <div v-if="preview" class="panel">
    <p>
      营业日 {{ preview.business_day }} · 笔数 {{ preview.line_count }} · 系统合计 {{ preview.system_total.toFixed(2) }}
      <span class="tag" :class="preview.sealed ? 'ok' : 'warn'">{{ preview.sealed ? '已日封（全日锁定）' : '未日封' }}</span>
      <span class="tag" :class="isTill ? 'ok' : 'warn'">
        {{ isTill ? '分机先封' : '整店日封' }}
      </span>
    </p>

    <!-- 分机维度：两种策略下均为只读汇总；仅分机先封策略可逐台机封 -->
    <div class="panel">
      <h2>分机汇总 <small style="font-weight:400;color:var(--steel)">（只读维度，不替代封账状态）</small></h2>
      <table>
        <thead>
          <tr><th>机具</th><th>笔数</th><th>系统合计</th><th>机封状态</th><th>机级长短款</th>
            <th v-if="isTill && !preview.sealed">实盘 / 机封</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in preview.till_summary" :key="t.till_code">
            <td>{{ t.till_code }} · {{ t.title }}</td>
            <td>{{ t.line_count }}</td>
            <td>{{ t.system_total.toFixed(2) }}</td>
            <td>
              <span v-if="t.till_sealed" class="tag ok">已机封 {{ t.till_batch_no }}</span>
              <span v-else class="tag warn">未机封</span>
            </td>
            <td>{{ t.till_variance == null ? '—' : t.till_variance.toFixed(2) }}</td>
            <td v-if="isTill && !preview.sealed">
              <div class="row" style="margin:0">
                <input type="number" step="0.01" v-model.number="tillDeclared[t.till_code]"
                  :disabled="t.till_sealed || t.line_count === 0" style="min-width:110px" />
                <button class="btn" :disabled="t.till_sealed || busy === t.till_code || t.line_count === 0"
                  @click="sealTill(t.till_code)">
                  {{ busy === t.till_code ? '封账中…' : '机封' }}
                </button>
              </div>
              <small v-if="t.line_count === 0" class="msg err" style="margin:0.2rem 0 0">零流水机不可机封，请在下方显式收口</small>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 整店日封策略：一次封账覆盖全日 -->
    <div v-if="!isTill" class="panel">
      <h2>整店日封</h2>
      <p class="msg">一次封账覆盖该营业日全部机具有效流水并锁定全日；实盘为整店现金，不产生机级批次。</p>
      <div class="row">
        <label>整店实盘现金 <input type="number" step="0.01" v-model.number="dayDeclared" :disabled="preview.sealed" /></label>
        <button class="btn" :disabled="preview.sealed || busy === 'day'" @click="sealDay">
          {{ busy === 'day' ? '封账中…' : '整店日封' }}
        </button>
        <button class="btn ghost" @click="load">刷新</button>
      </div>
    </div>

    <!-- 分机先封策略：校验通过后方可全店日封 -->
    <div v-else class="panel">
      <h2>全店日封（机封收口）</h2>
      <p class="msg">
        已封机合计 {{ preview.sealed_till_sum.toFixed(2) }} / 日系统合计 {{ preview.system_total.toFixed(2) }}
        <span class="tag" :class="preview.totals_match ? 'ok' : 'warn'">
          {{ preview.totals_match ? '合计可核平' : '合计不符' }}
        </span>
      </p>
      <p v-if="preview.missing_tills.length" class="msg err">
        待机封机具：{{ preview.missing_tills.join(', ') }}（缺机则日封失败）
      </p>
      <div v-if="preview.inactive_tills.length && !preview.sealed" class="row">
        <label v-for="code in preview.inactive_tills" :key="code" style="flex-direction:row;align-items:center;gap:0.4rem">
          <input type="checkbox" :style="{minWidth:'auto'}" v-model="waive[code]" />
          显式收口零流水机 {{ code }}
        </label>
      </div>
      <p v-if="preview.waived_tills.length" class="msg">已收口：{{ preview.waived_tills.join(', ') }}</p>
      <div class="row">
        <label>整店实盘现金 <input type="number" step="0.01" v-model.number="dayDeclared" :disabled="preview.sealed" /></label>
        <button class="btn" :disabled="daySealDisabled" @click="sealDay">
          {{ busy === 'day' ? '封账中…' : '全店日封' }}
        </button>
        <button class="btn ghost" @click="load">刷新</button>
      </div>
      <p v-if="!preview.can_day_seal && !preview.sealed" class="msg err">
        日封前置：当日营业机全部机封，且机封合计与日系统合计一致。
      </p>
      <p v-else-if="!waiveComplete && !preview.sealed" class="msg err">
        存在零流水机具未显式收口，请勾选收口后再日封。
      </p>
    </div>
  </div>

  <p v-if="msg" class="msg">{{ msg }}</p>
  <p v-if="err" class="msg err">{{ err }}</p>

  <div class="panel">
    <h2>封账批次</h2>
    <table>
      <thead>
        <tr><th>批次</th><th>营业日</th><th>范围</th><th>系统</th><th>实盘</th><th>差额</th><th>锁粒度</th><th>引用机批</th><th>状态</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="b in batches" :key="b.id">
          <td>{{ b.batch_no }}</td><td>{{ b.business_day }}</td>
          <td>{{ b.scope === 'day' ? '日级' : `机级·${b.till_code}` }}</td>
          <td>{{ b.system_total.toFixed(2) }}</td><td>{{ b.declared_cash.toFixed(2) }}</td>
          <td>{{ b.variance.toFixed(2) }}</td>
          <td>{{ b.locked_day ? '全日锁' : '单机锁' }}</td>
          <td>{{ b.scope === 'day' ? (tillRefHint(b).join(', ') || '—') : '—' }}</td>
          <td><span class="tag" :class="b.status === 'sealed' ? 'ok' : 'warn'">{{ b.status }}</span></td>
          <td>
            <button v-if="b.status === 'sealed'" class="btn ghost" :disabled="busy === `void-${b.id}`"
              @click="voidBatch(b)">
              {{ busy === `void-${b.id}` ? '作废中…' : '作废' }}
            </button>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
