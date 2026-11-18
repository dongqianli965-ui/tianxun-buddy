<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{
  result: unknown;
  error?: string;
  busy?: boolean;
  label?: string;
}>();

const pretty = computed(() => {
  if (props.result === null || props.result === undefined) return "";
  try { return JSON.stringify(props.result, null, 2); } catch { return String(props.result); }
});
</script>

<template>
  <div class="result-panel">
    <div class="result-head">
      <strong>{{ label || "响应" }}</strong>
      <span v-if="busy" class="badge pending">请求中…</span>
      <span v-else-if="error" class="badge bad">失败</span>
      <span v-else-if="result !== null && result !== undefined" class="badge ok">成功</span>
      <span v-else class="hint">尚未调用</span>
    </div>
    <p v-if="error" class="err">{{ error }}</p>
    <pre v-if="pretty">{{ pretty }}</pre>
    <slot />
  </div>
</template>
