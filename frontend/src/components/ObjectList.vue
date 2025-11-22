<script setup>
defineProps(['objects', 'selectedId']);
defineEmits(['select']);
</script>

<template>
  <div class="scrollable-list">
    <div 
        v-for="obj in objects" 
        :key="obj.name" 
        class="object-card" 
        :class="{ active: selectedId === obj.name }"
        @click="$emit('select', obj)"
    >
      <header>
        <h4>{{ obj.name }}
            <span :class="['status-badge', `status-${obj.status}`]">{{ obj.status }}</span>
        </h4>
      </header>
      <small>
        Cat: {{ obj.catalog }} | Mag: {{ obj.mag }} | Size: {{ obj.size }}
      </small>
      <br/>
      <small v-if="obj.hours_above_min > 0">
        Visible for {{ obj.hours_above_min }}h
      </small>
    </div>
    <div v-if="objects.length === 0" style="text-align: center; color: gray;">
        No objects loaded.
    </div>
  </div>
</template>