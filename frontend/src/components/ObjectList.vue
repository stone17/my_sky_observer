<script setup>
import { computed, ref, watch } from 'vue';

const props = defineProps(['objects', 'selectedId', 'settings']);
const emit = defineEmits(['select', 'update-settings']);

const activeDropdown = ref(null);
const localSettings = ref({});

const sortOptions = ref({
  time: { label: 'Best Time (Altitude)', enabled: true },
  hours_above: { label: 'Hours Visible', enabled: false },
  brightness: { label: 'Brightness', enabled: false },
  size: { label: 'Size', enabled: false }
});

// Sync local settings
watch(() => props.settings, (newVal) => {
    if (newVal) {
        localSettings.value = JSON.parse(JSON.stringify(newVal));
        if (localSettings.value.min_altitude === undefined) localSettings.value.min_altitude = 30.0;
        if (localSettings.value.min_hours === undefined) localSettings.value.min_hours = 0.0;

        const currentSort = localSettings.value.sort_key || 'time';
        const keys = currentSort.split(',');
        for (const k in sortOptions.value) {
            sortOptions.value[k].enabled = keys.includes(k);
        }
    }
}, { immediate: true, deep: true });

const toggleDropdown = () => {
  activeDropdown.value = activeDropdown.value ? null : 'filter';
};

const updateSort = () => {
    const keys = Object.keys(sortOptions.value).filter(k => sortOptions.value[k].enabled);
    if (keys.length === 0) {
        sortOptions.value.time.enabled = true;
        keys.push('time');
    }
    localSettings.value.sort_key = keys.join(',');
    emit('update-settings', localSettings.value);
};

// Helper to generate SVG path for altitude
const getAltitudePath = (altitudeGraph) => {
    if (!altitudeGraph || altitudeGraph.length === 0) return "";

    const width = 100;
    const height = 40; // Increased slightly
    const maxAlt = 90;

    const stepX = width / (altitudeGraph.length - 1);
    let d = `M 0 ${height}`;

    altitudeGraph.forEach((point, index) => {
        const x = index * stepX;
        const alt = point.altitude;
        const y = height - (Math.max(0, alt) / maxAlt) * height;
        d += ` L ${x} ${y}`;
    });

    d += ` L ${width} ${height} Z`;
    return d;
};
</script>

<template>
  <div class="list-wrapper">
    <!-- Filter Header (Always Visible) -->
    <div class="list-header">
         <div class="filter-row">
            <div class="field-group compact">
                 <label>Min Alt (°)</label>
                 <input type="number" v-model.number="localSettings.min_altitude" @change="$emit('update-settings', localSettings)" class="input-xs" />
            </div>
            <div class="field-group compact">
                 <label>Min Hours</label>
                 <input type="number" step="0.5" v-model.number="localSettings.min_hours" @change="$emit('update-settings', localSettings)" class="input-xs" />
            </div>
         </div>
         <div class="sort-row">
             <label>Sort:</label>
             <div class="sort-tags">
                 <span
                    v-for="(opt, key) in sortOptions"
                    :key="key"
                    class="sort-tag"
                    :class="{ active: opt.enabled }"
                    @click="opt.enabled = !opt.enabled; updateSort()"
                 >
                    {{ opt.label }}
                 </span>
             </div>
         </div>
    </div>

    <!-- Scrollable List -->
    <div class="scrollable-list">
        <div
            v-for="obj in objects"
            :key="obj.name"
            class="object-card"
            :class="{ active: selectedId === obj.name }"
            @click="$emit('select', obj)"
        >
          <div class="card-content">
              <!-- Left: Info -->
              <div class="info-col">
                  <header>
                    <h4>{{ obj.name }}</h4>
                  </header>
                  <small>
                    {{ obj.catalog }}<br/>
                    Mag: {{ obj.mag }}<br/>
                    Size: {{ obj.size }}<br/>
                    <span v-if="obj.hours_above_min > 0" class="vis-time">
                        Vis: {{ obj.hours_above_min }}h
                    </span>
                  </small>
                  <span :class="['status-badge', `status-${obj.status}`]">{{ obj.status }}</span>
              </div>

              <!-- Center: Image -->
              <div class="img-col">
                  <img v-if="obj.image_url" :src="obj.image_url" class="list-thumb" loading="lazy" />
                  <div v-else class="img-placeholder"></div>
              </div>

              <!-- Right: Graph -->
              <div class="graph-col">
                  <div class="altitude-chart" v-if="obj.altitude_graph && obj.altitude_graph.length">
                      <svg viewBox="0 0 100 40" preserveAspectRatio="none" width="100%" height="40">
                          <line x1="0" y1="26" x2="100" y2="26" stroke="#444" stroke-width="1" stroke-dasharray="2" />
                          <path :d="getAltitudePath(obj.altitude_graph)" fill="rgba(0, 255, 0, 0.2)" stroke="#0f0" stroke-width="1" />
                      </svg>
                  </div>
              </div>
          </div>
        </div>
        <div v-if="objects.length === 0" style="text-align: center; color: gray; padding: 20px;">
            No objects loaded.
        </div>
    </div>
  </div>
</template>

<style scoped>
.list-wrapper {
    display: flex;
    flex-direction: column;
    height: 100%;
    position: relative;
}

.list-header {
    background: #1f2937;
    border-bottom: 1px solid #374151;
    padding: 10px;
    z-index: 50;
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.filter-row {
    display: flex;
    gap: 10px;
}
.compact {
    flex: 1;
    display: flex;
    flex-direction: column;
}
.input-xs {
    width: 100%;
    padding: 4px;
    background: #000;
    border: 1px solid #4b5563;
    color: white;
    font-size: 0.85rem;
}

.sort-row {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 0.8rem;
    color: #9ca3af;
}
.sort-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
}
.sort-tag {
    background: #374151;
    padding: 2px 8px;
    border-radius: 4px;
    cursor: pointer;
    user-select: none;
    font-size: 0.75rem;
}
.sort-tag:hover { background: #4b5563; }
.sort-tag.active {
    background: #10b981;
    color: #000;
    font-weight: bold;
}

.scrollable-list {
    flex: 1;
    overflow-y: auto;
    padding: 5px;
}

.object-card {
    background: #111827;
    border: 1px solid #374151;
    margin-bottom: 5px;
    cursor: pointer;
    border-radius: 4px;
}
.object-card:hover { background: #1f2937; }
.object-card.active {
    border-color: #10b981;
    background: #1f2937;
}

.card-content {
    display: flex;
    height: 80px; /* Fixed height for consistency */
}

.info-col {
    flex: 1;
    padding: 8px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    overflow: hidden;
}

.img-col {
    width: 80px;
    background: #000;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
}

.list-thumb {
    width: 100%;
    height: 100%;
    object-fit: cover;
}

.img-placeholder {
    width: 100%;
    height: 100%;
    background: #222;
}

.graph-col {
    flex: 1;
    display: flex;
    align-items: center;
    padding: 5px;
    background: #000;
}

.info-col h4 {
    margin: 0 0 2px 0;
    font-size: 0.9rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.info-col small {
    font-size: 0.75rem;
    color: #9ca3af;
    line-height: 1.2;
}
.vis-time {
    color: #10b981;
    font-weight: bold;
}

.status-badge {
    font-size: 0.7rem;
    padding: 1px 4px;
    border-radius: 2px;
    background: #374151;
    margin-top: auto;
    align-self: flex-start;
}
.status-cached { color: #10b981; }
.status-downloading { color: #f59e0b; }
.status-error { color: #ef4444; }

.altitude-chart {
    width: 100%;
    height: 100%;
}

.field-group { margin-bottom: 5px; }
.field-group label { display: block; font-size: 0.8em; color: #9ca3af; }
.field-group input { width: 100%; padding: 5px; background: #000; border: 1px solid #444; color: white; }
.checkbox-row { display: flex; align-items: center; gap: 5px; margin-bottom: 5px; }
</style>