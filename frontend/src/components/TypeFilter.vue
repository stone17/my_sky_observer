<script setup>
const props = defineProps(['availableTypes', 'clientSettings']);
const emit = defineEmits(['update-client-settings']);
</script>

<template>
  <aside class="type-filter-panel">
    <div class="filter-header">
      <h3>Types</h3>
    </div>
    <div class="filter-list">
      <label 
          v-for="type in availableTypes" 
          :key="type" 
          class="type-pill"
          :class="{ active: clientSettings.selected_types.includes(type) }"
      >
          <input 
              type="checkbox" 
              :value="type" 
              v-model="clientSettings.selected_types" 
              @change="$emit('update-client-settings', clientSettings)"
              hidden
          >
          {{ type }}
      </label>
      <div v-if="!availableTypes || availableTypes.length === 0" class="empty-msg">
          No types found
      </div>
    </div>
  </aside>
</template>

<style scoped>
.type-filter-panel {
    width: 140px; /* Slightly narrower for pills */
    background: #111827;
    border-right: 1px solid #374151;
    display: flex;
    flex-direction: column;
    height: 100%;
    overflow: hidden;
}

.filter-header {
    padding: 10px;
    border-bottom: 1px solid #374151;
    background: #1f2937;
    text-align: center;
}

.filter-header h3 {
    margin: 0;
    font-size: 0.85rem;
    text-transform: uppercase;
    color: #9ca3af;
    letter-spacing: 0.05em;
}

.filter-list {
    flex: 1;
    overflow-y: auto;
    padding: 10px;
    display: flex;
    flex-direction: column;
    gap: 6px; /* Spacing between pills */
    align-items: stretch; /* Full width pills */
}

.type-pill {
    background: #374151;
    border: 1px solid #4b5563;
    color: #9ca3af;
    padding: 4px 10px;
    border-radius: 12px;
    font-size: 0.85rem;
    cursor: pointer;
    text-align: center;
    user-select: none;
    transition: all 0.2s;
}

.type-pill:hover {
    background: #4b5563;
    color: white;
}

.type-pill.active {
    background: #10b981;
    border-color: #059669;
    color: white;
}

.empty-msg {
    padding: 10px;
    color: #6b7280;
    font-size: 0.8rem;
    text-align: center;
}

/* Scrollbar styling */
.filter-list::-webkit-scrollbar {
    width: 6px;
}
.filter-list::-webkit-scrollbar-track {
    background: #111827;
}
.filter-list::-webkit-scrollbar-thumb {
    background: #374151;
    border-radius: 3px;
}
</style>
