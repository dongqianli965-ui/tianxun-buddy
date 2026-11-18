<script setup lang="ts">
import { computed } from "vue";
import { endpoint } from "../endpoints";
import { latestObservation, observationLabel } from "../diagnostics";

const props = defineProps<{ keys: string[]; title?: string }>();
const items = computed(() => props.keys.map(endpoint));
</script>

<template>
  <div class="endpoints">
    <div class="ep-title">
      {{ title || "端点静态登记与请求观察" }}
      <span class="hint">静态接线不代表实时可用；仅显示本次会话真实请求的最近状态</span>
    </div>
    <ul>
      <li v-for="m in items" :key="m.key">
        <span class="badge" :class="m.state">{{ m.state === "connected" ? "已登记接线" : "尚未实现" }}</span>
        <code>{{ m.method }} {{ m.path }}</code>
        <span class="ep-label">{{ m.label }}</span>
        <span class="badge" :class="latestObservation(m) ? (latestObservation(m)?.ok?'ok':'bad') : 'pending'">{{observationLabel(m)}}</span>
        <p v-if="m.state === 'unconnected'" class="ep-note">未接通原因：{{ m.note }}</p>
      </li>
    </ul>
  </div>
</template>
