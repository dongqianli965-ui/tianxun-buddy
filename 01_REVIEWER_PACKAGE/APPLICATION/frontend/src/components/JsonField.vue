<script setup lang="ts">
const props = defineProps<{
  modelValue: string;
  label: string;
  rows?: number;
  hint?: string;
  placeholder?: string;
}>();
const emit = defineEmits<{ (e: "update:modelValue", value: string): void }>();
</script>

<template>
  <label class="json-field">
    <span class="field-label">{{ label }}</span>
    <textarea
      :rows="rows || 6"
      :value="modelValue"
      :placeholder="placeholder"
      spellcheck="false"
      @input="emit('update:modelValue', ($event.target as HTMLTextAreaElement).value)"
    ></textarea>
    <span v-if="hint" class="hint">{{ hint }}</span>
  </label>
</template>
