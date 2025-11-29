<script setup>
import { computed, ref, watch, nextTick } from 'vue';

const props = defineProps(['objects', 'selectedId', 'settings', 'nightTimes']);
const emit = defineEmits(['select', 'update-settings', 'fetch-all']);

const activeDropdown = ref(null);
const localSettings = ref({});

const sortOptions = [
    { value: 'time', label: 'Best Time (Altitude)' },
    { value: 'hours_above', label: 'Longest Visibility' },
    { value: 'brightness', label: 'Brightest' },
    { value: 'size', label: 'Largest' }
];

// Auto-scroll to selected item
watch(() => props.selectedId, (newId) => {
    if (newId) {
        nextTick(() => {
            const el = document.getElementById(`obj-card-${newId}`);
            if (el) {
                el.scrollIntoView({ block: 'nearest' });
            }
        });
    }
});

// Sync local settings
watch(() => props.settings, (newVal) => {
    if (newVal) {
        localSettings.value = JSON.parse(JSON.stringify(newVal));
        // Defaults
        if (localSettings.value.min_altitude === undefined) localSettings.value.min_altitude = 30.0;
        if (localSettings.value.min_hours === undefined) localSettings.value.min_hours = 0.0;
        if (localSettings.value.max_magnitude === undefined) localSettings.value.max_magnitude = 12.0;
        if (localSettings.value.min_size === undefined) localSettings.value.min_size = 0.0;
        if (!localSettings.value.sort_key) localSettings.value.sort_key = 'time';
    }
}, { immediate: true, deep: true });

const updateSettings = () => {
    emit('update-settings', localSettings.value);
};

// Helper to calculate hours visible dynamically
const calculateHoursVisible = (altitudeGraph, minAltitude, nightTimes) => {
    if (!altitudeGraph || altitudeGraph.length === 0) return 0;
    
    // Strict astronomical night check
    if (!nightTimes || !nightTimes.night) return 0;

    const nightStart = new Date(nightTimes.night[0]);
    const nightEnd = new Date(nightTimes.night[1]);
    
    const validPoints = [];
    
    for (const p of altitudeGraph) {
        if (p.altitude >= minAltitude) {
            const pt = new Date(p.time);
            if (pt >= nightStart && pt <= nightEnd) {
                validPoints.push(p);
            }
        }
    }
    
    if (validPoints.length === 0) return 0;
    
    let step = 0.4; 
    if (altitudeGraph.length > 1) {
        const t1 = new Date(altitudeGraph[0].time);
        const t2 = new Date(altitudeGraph[1].time);
        step = (t2 - t1) / (1000 * 60 * 60); // hours
    }
    
    return Number((validPoints.length * step).toFixed(1));
};

const filteredAndSortedObjects = computed(() => {
    // Calculate dynamic visibility for all objects first
    let result = props.objects.map(o => {
        const dynamicHours = calculateHoursVisible(
            o.altitude_graph, 
            localSettings.value.min_altitude || 30, 
            props.nightTimes
        );
        return { ...o, dynamicHoursVisible: dynamicHours };
    });

    // 1. Filter
    // Min Hours
    if (localSettings.value.min_hours > 0) {
        result = result.filter(o => o.dynamicHoursVisible >= localSettings.value.min_hours);
    }
    
    // Min Altitude (Basic check against max_altitude)
    if (localSettings.value.min_altitude > 0) {
         result = result.filter(o => (o.max_altitude || 0) >= localSettings.value.min_altitude);
    }

    // Max Magnitude (Fainter than X is filtered out)
    // Note: Magnitude is inverse (lower is brighter). So "Max Magnitude 10" means filter out > 10.
    if (localSettings.value.max_magnitude !== undefined) {
        result = result.filter(o => (o.mag !== undefined && o.mag <= localSettings.value.max_magnitude));
    }

    // Min Size (Smaller than X is filtered out)
    if (localSettings.value.min_size > 0) {
        result = result.filter(o => (o.maj_ax || 0) >= localSettings.value.min_size);
    }

    // 2. Sort
    const key = localSettings.value.sort_key || 'time';

    result.sort((a, b) => {
        let valA, valB;
        let dir = -1; // Default descending (larger is better)

        if (key === 'time' || key === 'altitude') {
            valA = a.max_altitude || 0;
            valB = b.max_altitude || 0;
        } else if (key === 'hours_above') {
            valA = a.dynamicHoursVisible || 0;
            valB = b.dynamicHoursVisible || 0;
        } else if (key === 'brightness') {
            valA = a.mag || 99;
            valB = b.mag || 99;
            dir = 1; // Ascending for magnitude (lower is better)
        } else if (key === 'size') {
            valA = a.maj_ax || 0;
            valB = b.maj_ax || 0;
        }

        if (valA !== valB) {
            return (valA - valB) * dir;
        }
        return 0;
    });

    return result;
});

// Helper to generate SVG path for altitude
const getAltitudePath = (altitudeGraph) => {
    if (!altitudeGraph || altitudeGraph.length === 0) return "";

    const width = 100;
    const height = 40; 
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
    <!-- Filter Header -->
    <div class="list-header">
         <div class="filter-grid">
            <div class="field-group compact">
                 <label>Min Alt (°)</label>
                 <input type="number" v-model.number="localSettings.min_altitude" @change="updateSettings" class="input-xs" />
            </div>
            <div class="field-group compact">
                 <label>Min Hours</label>
                 <input type="number" step="0.5" v-model.number="localSettings.min_hours" @change="updateSettings" class="input-xs" />
            </div>
            <div class="field-group compact">
                 <label>Max Mag</label>
                 <input type="number" step="0.5" v-model.number="localSettings.max_magnitude" @change="updateSettings" class="input-xs" />
            </div>
            <div class="field-group compact">
                 <label>Min Size (')</label>
                 <input type="number" step="1" v-model.number="localSettings.min_size" @change="updateSettings" class="input-xs" />
            </div>
         </div>
         
         <div class="sort-row">
             <label>Sort:</label>
             <select v-model="localSettings.sort_key" @change="updateSettings" class="sort-select">
                 <option v-for="opt in sortOptions" :key="opt.value" :value="opt.value">
                     {{ opt.label }}
                 </option>
             </select>
             
             <button
                v-if="localSettings.download_mode === 'filtered'"
                class="small primary"
                style="margin-left: auto;"
                @click="$emit('fetch-all')"
             >
                Fetch All
             </button>
         </div>
    </div>

    <!-- Scrollable List -->
    <div class="scrollable-list">
        <div
            v-for="obj in filteredAndSortedObjects"
            :key="obj.name"
            :id="`obj-card-${obj.name}`"
            class="object-card"
            :class="{ active: selectedId === obj.name }"
            @click="$emit('select', obj)"
        >
          <div class="card-content">
              <!-- Left: Info -->
              <div class="info-col">
                  <h4 :title="obj.name">{{ obj.name || 'Unknown Object' }}</h4>
                  <small>
                    Mag: {{ obj.mag }}<br/>
                    Size: {{ obj.size }}<br/>
                    <span class="vis-time">
                        Vis: {{ obj.dynamicHoursVisible || 0 }}h
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
        <div v-if="filteredAndSortedObjects.length === 0" style="text-align: center; color: gray; padding: 20px;">
            No objects match filters.
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

.filter-grid {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr 1fr;
    gap: 5px;
}

.compact {
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

.sort-select {
    background: #000;
    color: white;
    border: 1px solid #4b5563;
    padding: 2px 5px;
    font-size: 0.85rem;
    flex: 1;
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
    font-size: 1rem;
    font-weight: bold;
    line-height: 1.2;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    color: #f3f4f6;
    flex-shrink: 0; /* Prevent collapsing */
    min-height: 1.2em; /* Ensure height */
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
</style>