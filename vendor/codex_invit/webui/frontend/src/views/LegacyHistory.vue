<script setup>
import { ref, watch, onMounted } from 'vue'
import http from '@/api/request'
const kind = ref('runs'), items = ref([]), page = ref(1), total = ref(0), loading = ref(false)
async function load() {
  loading.value = true
  try {
    const response = await http.get('/api/v1/admin/invitation/legacy/' + kind.value, { baseURL: '', params: { page: page.value, pageSize: 20 } })
    items.value = response.data.items; total.value = response.data.total
  } finally { loading.value = false }
}
watch(kind, () => { page.value = 1; load() }); onMounted(load)
</script>
<template>
  <el-card>
    <template #header>迁移前的邀请历史</template>
    <el-alert title="保留原记录和成功依据。执行中及结果不明的任务不会因迁移自动重跑。" type="info" :closable="false" />
    <el-select v-model="kind" style="margin: 16px 0">
      <el-option v-for="option in [['runs','执行记录'],['events','执行事件'],['verifications','奖励核验记录'],['mail-accounts','原邮箱记录'],['keys','原卡密记录'],['proxies','原代理记录']]" :key="option[0]" :value="option[0]" :label="option[1]" />
    </el-select>
    <el-table v-loading="loading" :data="items">
      <el-table-column label="原始记录（敏感凭证仅显示是否已保存）"><template #default="scope"><pre style="white-space: pre-wrap; overflow-wrap: anywhere">{{ JSON.stringify(scope.row, null, 2) }}</pre></template></el-table-column>
    </el-table>
    <el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next, total" @current-change="load" />
  </el-card>
</template>
