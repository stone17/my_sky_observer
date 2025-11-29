<script setup>
import { computed, ref, watch, nextTick } from 'vue';

const props = defineProps(['objects', 'selectedId', 'settings', 'clientSettings', 'nightTimes']);
const emit = defineEmits(['select', 'fetch-all', 'update-settings', 'update-client-settings']);

const searchQuery = ref('');
const showSuggestions = ref(false);

// Local state for filters to handle v-model updates
const localSettings = ref({});
const localClientSettings = ref({});

const sortOptions = [
    { value: 'time', label: 'Best Time' },
    { value: 'hours_above', label: 'Longest Vis' },
    { value: 'brightness', label: 'Brightest' },
    { value: 'size', label: 'Largest' }
];

// Sync props to local state
watch(() => props.settings, (newVal) => {
    if (newVal) {
        localSettings.value = { ...newVal };
        if (localSettings.value.min_altitude === undefined) localSettings.value.min_altitude = 30.0;
        if (localSettings.value.min_hours === undefined) localSettings.value.min_hours = 0.0;
        if (!localSettings.value.sort_key) localSettings.value.sort_key = 'time';
    }
}, { immediate: true, deep: true });

watch(() => props.clientSettings, (newVal) => {
    if (newVal) {
        localClientSettings.value = { ...newVal };
    }
}, { immediate: true, deep: true });

const updateSettings = () => {
    emit('update-settings', localSettings.value);
};

const updateClientSettings = () => {
    emit('update-client-settings', localClientSettings.value);
};

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

const suggestionList = computed(() => {
    const q = searchQuery.value.trim();
    // Regex: Starts with letters, optional space, then digits (e.g. "M 3", "ngc 2")
    if (!q || !/[a-zA-Z]+\s*\d+/.test(q)) return [];

    const lowerQ = q.toLowerCase().replace(/\s+/g, '');
    
    // Filter objects that match
    const matches = props.objects.filter(o => {
        const name = o.name.toLowerCase().replace(/\s+/g, '');
        return name.startsWith(lowerQ);
    });

    // Return top 10 names
    return matches.slice(0, 10).map(o => o.name);
});

const selectSuggestion = (name) => {
    searchQuery.value = name;
    showSuggestions.value = false;
    // Also try to select it if it exists
    const obj = props.objects.find(o => o.name === name);
    if (obj) emit('select', obj);
};

const filteredAndSortedObjects = computed(() => {
    const minAlt = props.settings?.min_altitude || 30;
    const minHrs = props.settings?.min_hours || 0;
    const maxMag = props.clientSettings?.max_magnitude ?? 12;
    const minSize = props.clientSettings?.min_size || 0;
    const sortKey = props.settings?.sort_key || 'time';

    // Calculate dynamic visibility for all objects first
    let result = props.objects.map(o => {
        const dynamicHours = calculateHoursVisible(
            o.altitude_graph, 
            minAlt, 
            props.nightTimes
        );
        return { ...o, dynamicHoursVisible: dynamicHours };
    });

    const q = searchQuery.value.trim().toLowerCase();

    // 1. Filter
    result = result.filter(o => {
        // Search Bypass: If name matches search query (partial match), include it regardless of other filters
        if (q && o.name.toLowerCase().includes(q)) {
            return true;
        }

        // Standard Filters
        if (minHrs > 0 && o.dynamicHoursVisible < minHrs) return false;
        if (minAlt > 0 && (o.max_altitude || 0) < minAlt) return false;
        
        // Max Magnitude (Fainter than X is filtered out)
        if (maxMag !== undefined && o.mag !== undefined && o.mag > maxMag) return false;

        // Min Size (Smaller than X is filtered out)
        if (minSize > 0 && (o.maj_ax || 0) < minSize) return false;

        return true;
    });

    // 2. Sort
    result.sort((a, b) => {
        let valA, valB;
        let dir = -1; // Default descending (larger is better)

        if (sortKey === 'time' || sortKey === 'altitude') {
            valA = a.max_altitude || 0;
            valB = b.max_altitude || 0;
        } else if (sortKey === 'hours_above') {
            valA = a.dynamicHoursVisible || 0;
            valB = b.dynamicHoursVisible || 0;
        } else if (sortKey === 'brightness') {
            valA = a.mag || 99;
            valB = b.mag || 99;
            dir = 1; // Ascending for magnitude (lower is better)
        } else if (sortKey === 'size') {
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
    <!-- Left Sidebar: Filters -->
    <div class="filter-sidebar">
        <div class="filter-group">
            <label>Alt></label>
            <input type="number" v-model.number="localSettings.min_altitude" @change="updateSettings" class="input-sm" />
        </div>
        <div class="filter-group">
            <label>Hrs></label>
            <input type="number" step="0.5" v-model.number="localSettings.min_hours" @change="updateSettings" class="input-sm" />
        </div>
        <div class="filter-group">
            <label>Mag<</label>
            <input type="number" step="0.5" v-model.number="localClientSettings.max_magnitude" @change="updateClientSettings" class="input-sm" />
        </div>
        <div class="filter-group">
            <label>Size></label>
            <input type="number" step="1" v-model.number="localClientSettings.min_size" @change="updateClientSettings" class="input-sm" />
        </div>
        <div class="filter-group">
            <label>Sort</label>
            <select v-model="localSettings.sort_key" @change="updateSettings" class="input-sm sort-select">
                <option v-for="opt in sortOptions" :key="opt.value" :value="opt.value">
                    {{ opt.label }}
                </option>
            </select>
        </div>
        
        <button
           v-if="localSettings.download_mode === 'filtered'"
           class="small primary fetch-btn"
           @click="$emit('fetch-all')"
        >
           Fetch All
        </button>
    </div>

    <!-- Right Content: Search + List -->
    <div class="list-content">
        <!-- Search Box -->
        <div class="list-header">
             <div class="search-box">
                 <input 
                    type="text" 
                    v-model="searchQuery" 
                    placeholder="Search object..." 
                    class="search-input"
                    @focus="showSuggestions = true"
                    @blur="setTimeout(() => showSuggestions = false, 200)"
                 />
                 <div v-if="showSuggestions && suggestionList.length > 0" class="suggestions-dropdown">
                     <div 
                        v-for="s in suggestionList" 
                        :key="s" 
                        class="suggestion-item"
                        @click="selectSuggestion(s)"
                     >
                        {{ s }}
                     </div>
                 </div>
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
  </div>
</template>

<style scoped>
.list-wrapper {
    display: flex;
    flex-direction: row; /* Side by side */
    height: 100%;
    position: relative;
    overflow: hidden;
}

.filter-sidebar {
    width: 100px;
    background: #111827;
    border-right: 1px solid #374151;
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 12px 6px;
    align-items: center;
    overflow-y: auto;
}

.list-content {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

.list-header {
    background: #1f2937;
    border-bottom: 1px solid #374151;
    padding: 10px;
    z-index: 50;
}

.search-box {
    position: relative;
    width: 100%;
}

.search-input {
    width: 100%;
    padding: 6px;
    background: #000;
    border: 1px solid #4b5563;
    color: white;
    font-size: 0.9rem;
    border-radius: 4px;
}

.suggestions-dropdown {
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    background: #1f2937;
    border: 1px solid #4b5563;
    z-index: 100;
    max-height: 200px;
    overflow-y: auto;
    box-shadow: 0 4px 6px rgba(0,0,0,0.3);
}

.suggestion-item {
    padding: 8px;
    cursor: pointer;
    border-bottom: 1px solid #374151;
}
.suggestion-item:hover {
    background: #374151;
}

.scrollable-list {
    flex: 1;
    overflow-y: auto;
    padding: 5px;
}

.filter-group {
    display: flex;
    flex-direction: column;
    align-items: center;
    width: 100%;
    gap: 2px;
}

.filter-group label {
    font-size: 0.8rem;
    color: #9ca3af;
    text-align: center;
    margin-bottom: 2px;
}

.input-sm {
    width: 100%;
    background: #000;
    color: white;
    border: 1px solid #4b5563;
    padding: 4px;
    font-size: 0.9rem;
    text-align: center;
}

.sort-select {
    text-align: left;
    font-size: 0.85rem;
    padding: 4px;
}

.fetch-btn {
    width: 100%;
    font-size: 0.85rem;
    padding: 6px 4px;
    margin-top: 10px;
    text-align: center;
    white-space: normal;
    line-height: 1.2;
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
</style>