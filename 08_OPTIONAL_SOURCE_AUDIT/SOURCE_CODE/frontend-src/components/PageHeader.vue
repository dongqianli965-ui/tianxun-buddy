<script setup lang="ts">
defineProps<{ title: string; desc?: string }>();
</script>

<template>
  <div class="page-head">
    <div>
      <h2>{{ title }}</h2>
      <p v-if="desc" class="page-desc">{{ desc }}</p>
    </div>
    <div class="page-actions"><slot /></div>
  </div>
</template>
