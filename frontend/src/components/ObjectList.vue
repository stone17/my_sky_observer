<script setup>
import { computed, ref, watch, nextTick } from 'vue';

const props = defineProps(['objects', 'selectedId', 'settings', 'clientSettings', 'nightTimes', 'searchQuery']);
const emit = defineEmits(['select', 'fetch-all', 'update-settings', 'update-client-settings']);

const showInfoModal = ref(false);
const infoObject = ref(null);

const localSettings = ref({});
const localClientSettings = ref({});

const sortOptions = [
    { value: 'time', label: 'Altitude' },
    { value: 'hours_above', label: 'Visible' },
    { value: 'brightness', label: 'Brightness' },
    { value: 'size', label: 'Size' }
];

watch(() => props.settings, (newVal) => {
    if (newVal && JSON.stringify(newVal) !== JSON.stringify(localSettings.value)) {
        localSettings.value = { ...newVal };
        if (localSettings.value.min_altitude === undefined) localSettings.value.min_altitude = 30.0;
        if (!localSettings.value.sort_key) localSettings.value.sort_key = 'time';
    }
}, { immediate: true, deep: true });

watch(() => props.clientSettings, (newVal) => {
    if (newVal) localClientSettings.value = { ...newVal };
}, { immediate: true, deep: true });

const updateSettings = () => emit('update-settings', localSettings.value);
const updateClientSettings = () => emit('update-client-settings', localClientSettings.value);

watch(() => props.selectedId, (newId) => {
    if (newId) {
        nextTick(() => {
            const el = document.getElementById(`obj-card-${newId}`);
            if (el) el.scrollIntoView({ block: 'nearest' });
        });
    }
});

const openInfo = (obj) => {
    infoObject.value = obj;
    showInfoModal.value = true;
};

const filteredAndSortedObjects = computed(() => {
    const minAlt = props.settings?.min_altitude || 30;
    const minHrs = props.clientSettings?.min_hours || 0;
    const maxMag = props.clientSettings?.max_magnitude ?? 12;
    const minSize = props.clientSettings?.min_size || 0;
    const sortKey = props.settings?.sort_key || 'time';

    const q = (props.searchQuery || '').trim().toLowerCase();
    const normQ = q.replace(/\s+/g, '');

    let result = [];

    for (const o of props.objects) {
        if (normQ) {
            const normId = o.name.toLowerCase().replace(/\s+/g, '');
            const normCommon = (o.common_name || '').toLowerCase().replace(/\s+/g, '');
            const normOther = (o.other_id || '').toLowerCase().replace(/\s+/g, '');

            if (normId.includes(normQ) || normCommon.includes(normQ) || normOther.includes(normQ)) {
                o._dynamicHours = o.hours_visible || 0;
                result.push(o);
            }
            continue;
        }

        if (minHrs > 0 && (o.hours_visible || 0) < minHrs) continue;
        if (minAlt > 0 && (o.max_altitude || 0) < minAlt) continue;
        if (maxMag !== undefined && o.mag !== undefined && o.mag > maxMag) continue;
        if (minSize > 0 && (o.maj_ax || 0) < minSize) continue;

        const selectedTypes = props.clientSettings?.selected_types || [];
        if (selectedTypes.length > 0 && !selectedTypes.includes(o.type)) continue;

        o._dynamicHours = o.hours_visible || 0;
        result.push(o);
    }

    result.sort((a, b) => {
        if (normQ) {
            const normA = a.name.toLowerCase().replace(/\s+/g, '');
            const normB = b.name.toLowerCase().replace(/\s+/g, '');

            if (normA === normQ && normB !== normQ) return -1;
            if (normB === normQ && normA !== normQ) return 1;

            const aStarts = normA.startsWith(normQ);
            const bStarts = normB.startsWith(normQ);
            if (aStarts && !bStarts) return -1;
            if (bStarts && !aStarts) return 1;

            if (normA.length !== normB.length) return normA.length - normB.length;
            return normA.localeCompare(normB);
        }

        let valA, valB, dir = -1;

        if (sortKey === 'time' || sortKey === 'altitude') {
            valA = a.max_altitude || 0;
            valB = b.max_altitude || 0;
        } else if (sortKey === 'hours_above') {
            valA = a._dynamicHours || 0;
            valB = b._dynamicHours || 0;
        } else if (sortKey === 'brightness') {
            valA = a.mag || 99;
            valB = b.mag || 99;
            dir = 1;
        } else if (sortKey === 'size') {
            valA = a.maj_ax || 0;
            valB = b.maj_ax || 0;
        }

        if (valA !== valB) return (valA - valB) * dir;
        return 0;
    });

    return result;
});

const getAltitudePath = (altitudeGraph) => {
    if (!altitudeGraph || altitudeGraph.length < 2) return "";
    const width = 100;
    const height = 40;
    const maxAlt = 90;
    const stepX = width / (altitudeGraph.length - 1);
    let d = `M 0 ${height}`;
    altitudeGraph.forEach((point, index) => {
        const x = index * stepX;
        const y = height - (Math.max(0, point.altitude) / maxAlt) * height;
        d += ` L ${x} ${y}`;
    });
    d += ` L ${width} ${height} Z`;
    return d;
};
</script>

<template>
    <div class="list-wrapper">
        <div class="filter-sidebar">
            <div class="filter-group">
                <label>Alt></label>
                <input type="number" v-model.number="localSettings.min_altitude" @input="updateSettings"
                    class="input-sm" />
            </div>
            <div class="filter-group">
                <label>Hrs&gt;</label>
                <input type="number" step="0.5" v-model.number="localClientSettings.min_hours"
                    @change="updateClientSettings" class="input-sm" />
            </div>
            <div class="filter-group">
                <label>Mag&lt;</label>
                <input type="number" step="0.5" v-model.number="localClientSettings.max_magnitude"
                    @change="updateClientSettings" class="input-sm" />
            </div>
            <div class="filter-group">
                <label>Size></label>
                <input type="number" step="1" v-model.number="localClientSettings.min_size"
                    @change="updateClientSettings" class="input-sm" />
            </div>
            <div class="filter-group">
                <label>Sort</label>
                <select v-model="localSettings.sort_key" @change="updateSettings" class="input-sm sort-select">
                    <option v-for="opt in sortOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                </select>
            </div>
            <button v-if="localSettings.download_mode === 'filtered'" class="small primary fetch-btn"
                @click="$emit('fetch-all')">Fetch All</button>
        </div>

        <div class="list-content">
            <div class="list-header">
                <div class="list-stats">
                    Showing {{ filteredAndSortedObjects.length }} / {{ props.objects.length }}
                </div>
            </div>

            <div class="scrollable-list">
                <div v-for="item in filteredAndSortedObjects" :key="item.name" :id="`obj-card-${item.name}`"
                    class="object-card" :class="{ active: selectedId === item.name }" @click="$emit('select', item)">
                    <div class="card-content">
                        <div class="info-col">
                            <div style="display: flex; align-items: center; gap: 5px; width: 100%;">
                                <h4 :title="item.name">{{ item.name }}</h4>
                                <div class="info-icon" @click.stop="openInfo(item)">ⓘ</div>
                            </div>
                            <div v-if="item.common_name && item.common_name !== 'N/A'" class="common-name"
                                :title="item.common_name">
                                {{ item.common_name }}
                            </div>
                            <small>
                                Mag: {{ item.mag }}<br />
                                Size: {{ item.size }}<br />
                                <span class="vis-time">Vis: {{ item._dynamicHours || 0 }}h</span>
                            </small>
                            <span :class="['status-badge', `status-${item.status}`]">{{ item.status }}</span>
                        </div>
                        <div class="img-col">
                            <img v-if="item.image_url" :src="item.image_url" class="list-thumb" loading="lazy" />
                            <div v-else class="img-placeholder"></div>
                        </div>
                        <div class="graph-col">
                            <div class="altitude-chart" v-if="item.altitude_graph && item.altitude_graph.length">
                                <svg viewBox="0 0 100 40" preserveAspectRatio="none" width="100%" height="40">
                                    <line x1="0" y1="26" x2="100" y2="26" stroke="#444" stroke-width="1"
                                        stroke-dasharray="2" />
                                    <path :d="getAltitudePath(item.altitude_graph)" fill="rgba(0, 255, 0, 0.2)"
                                        stroke="#0f0" stroke-width="1" />
                                </svg>
                            </div>
                        </div>
                    </div>
                </div>
                <div v-if="filteredAndSortedObjects.length === 0"
                    style="text-align: center; color: gray; padding: 20px;">
                    No objects match filters.
                </div>
            </div>
        </div>

        <div v-if="showInfoModal" class="modal-overlay" @click="showInfoModal = false">
            <div class="modal-content" @click.stop>
                <h3>{{ infoObject?.name }}</h3>
                <p v-if="infoObject?.common_name && infoObject?.common_name !== 'N/A'"><strong>Name:</strong> {{
                    infoObject.common_name }}</p>
                <p><strong>Type:</strong> {{ infoObject?.type }}</p>
                <p><strong>Constellation:</strong> {{ infoObject?.constellation }}</p>
                <p><strong>Magnitude:</strong> {{ infoObject?.mag }}</p>
                <p><strong>Size:</strong> {{ infoObject?.size }}</p>
                <p><strong>Surface Brightness:</strong> {{ infoObject?.surface_brightness }}</p>
                <button @click="showInfoModal = false" class="close-btn">Close</button>
            </div>
        </div>
    </div>
</template>

<style scoped>
.list-wrapper {
    display: flex;
    flex-direction: row;
    height: 100%;
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
}

.object-card {
    background: #111827;
    border: 1px solid #374151;
    margin-bottom: 5px;
    cursor: pointer;
    border-radius: 4px;
}

.object-card:hover {
    background: #1f2937;
}

.object-card.active {
    border-color: #10b981;
    background: #1f2937;
}

.card-content {
    display: flex;
    height: 80px;
}

/* FIX: Info column flex 2, graph column flex 1 */
.info-col {
    flex: 2;
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

.graph-col {
    flex: 1;
    display: flex;
    align-items: center;
    padding: 5px;
    background: #000;
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

.info-col h4 {
    margin: 0 0 2px 0;
    font-size: 1rem;
    font-weight: bold;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    color: #f3f4f6;
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

.status-cached {
    color: #10b981;
}

.status-downloading {
    color: #f59e0b;
}

.status-error {
    color: #ef4444;
}

.altitude-chart {
    width: 100%;
    height: 100%;
}

.info-icon {
    cursor: help;
    color: #60a5fa;
    font-size: 0.9rem;
}

.common-name {
    font-size: 0.8rem;
    color: #10b981;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    margin-bottom: 2px;
}

.list-stats {
    font-size: 0.75rem;
    color: #9ca3af;
    text-align: right;
    margin-top: 4px;
}

.modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.7);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
}

.modal-content {
    background: #1f2937;
    padding: 20px;
    border-radius: 8px;
    border: 1px solid #374151;
    max-width: 300px;
    width: 90%;
    color: #f3f4f6;
}

.modal-content h3 {
    margin-top: 0;
    border-bottom: 1px solid #374151;
    padding-bottom: 10px;
    margin-bottom: 10px;
}

.close-btn {
    margin-top: 15px;
    width: 100%;
    padding: 8px;
    background: #374151;
    border: none;
    color: white;
    border-radius: 4px;
    cursor: pointer;
}

.close-btn:hover {
    background: #4b5563;
}
</style>