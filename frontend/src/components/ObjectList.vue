<script setup>
import { computed, ref, watch, nextTick, onMounted, onUnmounted } from 'vue';
import {
    getObjectTypeLabel,
    getObjectTypeShort,
    getObjectTypeDescription,
    formatMagnitude
} from '../utils/catalogMeta.js';

const props = defineProps(['objects', 'selectedId', 'settings', 'clientSettings', 'nightTimes', 'searchQuery']);
const emit = defineEmits(['select', 'fetch-all', 'update-settings', 'update-client-settings', 'update-search']);

const showInfoModal = ref(false);
const infoObject = ref(null);
const showSuggestions = ref(false);

const localSettings = ref({});
const localClientSettings = ref({});
const renderedLimit = ref(50);

const sortOptions = [
    { value: 'time', label: 'Peak Altitude' },
    { value: 'hours_above', label: 'Visible Hours' },
    { value: 'brightness', label: 'Brightness (Mag)' },
    { value: 'size', label: 'Angular Size' }
];

watch(() => props.settings, (newVal) => {
    if (newVal && JSON.stringify(newVal) !== JSON.stringify(localSettings.value)) {
        localSettings.value = { ...newVal };
        if (localSettings.value.min_altitude === undefined) localSettings.value.min_altitude = 30.0;
        if (!localSettings.value.sort_key) localSettings.value.sort_key = 'time';
    }
}, { immediate: true, deep: true });

watch(() => props.clientSettings, (newVal) => {
    if (newVal) {
        localClientSettings.value = { ...newVal };
        if (localClientSettings.value.filter_active_alt === undefined) localClientSettings.value.filter_active_alt = true;
        if (localClientSettings.value.filter_na_alt === undefined) localClientSettings.value.filter_na_alt = true;
        if (localClientSettings.value.filter_active_hours === undefined) localClientSettings.value.filter_active_hours = true;
        if (localClientSettings.value.filter_na_hours === undefined) localClientSettings.value.filter_na_hours = true;
        if (localClientSettings.value.filter_active_mag === undefined) localClientSettings.value.filter_active_mag = true;
        if (localClientSettings.value.filter_na_mag === undefined) localClientSettings.value.filter_na_mag = true;
        if (localClientSettings.value.filter_active_size === undefined) localClientSettings.value.filter_active_size = true;
        if (localClientSettings.value.filter_na_size === undefined) localClientSettings.value.filter_na_size = true;
    }
}, { immediate: true, deep: true });

const updateSettings = () => emit('update-settings', localSettings.value);

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

const onSearchInput = (e) => {
    emit('update-search', e.target.value);
    showSuggestions.value = true;
};

const clearSearch = () => {
    emit('update-search', '');
    showSuggestions.value = false;
};

const selectSuggestion = (obj) => {
    emit('update-search', obj.name);
    handleObjectClick(obj);
    showSuggestions.value = false;
};

const onSearchBlur = () => {
    setTimeout(() => { showSuggestions.value = false; }, 200);
};

const suggestionList = computed(() => {
    const q = (props.searchQuery || '').trim();
    if (!q) return [];
    const normQ = q.toLowerCase().replace(/\s+/g, '');
    const matches = (props.objects || []).filter(o => {
        if (o._normId === undefined) o._normId = (o.name || '').toLowerCase().replace(/\s+/g, '');
        if (o._normCommon === undefined) o._normCommon = (o.common_name || '').toLowerCase().replace(/\s+/g, '');
        if (o._normOther === undefined) o._normOther = (o.other_id || '').toLowerCase().replace(/\s+/g, '');
        if (o._normAliases === undefined) o._normAliases = (o.aliases || '').toLowerCase().replace(/\s+/g, '');
        if (o._normAliasTokens === undefined) {
            o._normAliasTokens = (o.aliases || '')
                .split(',')
                .map(t => t.toLowerCase().replace(/\s+/g, ''))
                .filter(Boolean);
        }
        
        return o._normId.includes(normQ) || o._normCommon.includes(normQ) || o._normOther.includes(normQ) || o._normAliases.includes(normQ);
    });
    return matches.slice(0, 8);
});

const filteredAndSortedObjects = computed(() => {
    const minAlt = localSettings.value.min_altitude || 30;
    const activeAlt = localClientSettings.value.filter_active_alt !== false;
    const naAlt = localClientSettings.value.filter_na_alt !== false;

    const minHrs = localClientSettings.value.min_hours || 0;
    const activeHrs = localClientSettings.value.filter_active_hours !== false;
    const naHrs = localClientSettings.value.filter_na_hours !== false;

    const maxMag = localClientSettings.value.max_magnitude ?? 12;
    const activeMag = localClientSettings.value.filter_active_mag !== false;
    const naMag = localClientSettings.value.filter_na_mag !== false;

    const minSize = localClientSettings.value.min_size || 0;
    const activeSize = localClientSettings.value.filter_active_size !== false;
    const naSize = localClientSettings.value.filter_na_size !== false;

    const sortKey = localSettings.value.sort_key || 'time';

    const q = (props.searchQuery || '').trim().toLowerCase();
    const normQ = q.replace(/\s+/g, '');

    let result = [];

    for (const o of props.objects) {
        if (o._normId === undefined) o._normId = (o.name || '').toLowerCase().replace(/\s+/g, '');
        if (o._normCommon === undefined) o._normCommon = (o.common_name || '').toLowerCase().replace(/\s+/g, '');
        if (o._normOther === undefined) o._normOther = (o.other_id || '').toLowerCase().replace(/\s+/g, '');
        if (o._normAliases === undefined) o._normAliases = (o.aliases || '').toLowerCase().replace(/\s+/g, '');

        let isMatch = false;
        if (normQ) {
            if (o._normId.includes(normQ) || o._normCommon.includes(normQ) || o._normOther.includes(normQ) || o._normAliases.includes(normQ)) {
                isMatch = true;
            }
        }

        // If it's not a search match, apply normal filters.
        if (!isMatch) {
            if (activeHrs) {
                const isNaHrs = o.hours_visible === undefined || o.hours_visible === null;
                if (isNaHrs) {
                    if (!naHrs) continue;
                } else {
                    if (minHrs > 0 && o.hours_visible < minHrs) continue;
                }
            }

            if (activeAlt) {
                const isNaAlt = o.max_altitude === undefined || o.max_altitude === null;
                if (isNaAlt) {
                    if (!naAlt) continue;
                } else {
                    if (minAlt > 0 && o.max_altitude < minAlt) continue;
                }
            }

            if (activeMag) {
                const isNaMag = o.mag === undefined || o.mag === null || o.mag >= 99;
                if (isNaMag) {
                    if (!naMag) continue;
                } else {
                    if (maxMag !== undefined && o.mag > maxMag) continue;
                }
            }

            if (activeSize) {
                const isNaSize = o.maj_ax === undefined || o.maj_ax === null || o.maj_ax === 0;
                if (isNaSize) {
                    if (!naSize) continue;
                } else {
                    if (minSize > 0 && o.maj_ax < minSize) continue;
                }
            }

            const selectedTypes = localClientSettings.value.selected_types || [];
            if (selectedTypes.includes('__NONE__')) continue;
            if (selectedTypes.length > 0 && !selectedTypes.includes(o.type)) continue;
        }

        o._dynamicHours = o.hours_visible || 0;
        o._isSearchMatch = isMatch;
        result.push(o);
    }

    result.sort((a, b) => {
        if (normQ) {
            if (a._isSearchMatch && !b._isSearchMatch) return -1;
            if (!a._isSearchMatch && b._isSearchMatch) return 1;

            if (a._isSearchMatch && b._isSearchMatch) {
                const normA = a._normId;
                const normB = b._normId;

                const exactA = normA === normQ || (a._normAliasTokens && a._normAliasTokens.includes(normQ));
                const exactB = normB === normQ || (b._normAliasTokens && b._normAliasTokens.includes(normQ));
                if (exactA && !exactB) return -1;
                if (exactB && !exactA) return 1;

                const aStarts = normA.startsWith(normQ);
                const bStarts = normB.startsWith(normQ);
                if (aStarts && !bStarts) return -1;
                if (bStarts && !aStarts) return 1;

                if (normA.length !== normB.length) return normA.length - normB.length;
                return normA.localeCompare(normB);
            }
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

const displayedObjects = computed(() => {
    return filteredAndSortedObjects.value.slice(0, renderedLimit.value);
});

const typePoolCount = computed(() => {
    const selectedTypes = localClientSettings.value.selected_types || [];
    if (selectedTypes.length === 0) return (props.objects || []).length;
    if (selectedTypes.includes('__NONE__')) return 0;
    const activeSet = new Set(selectedTypes);
    return (props.objects || []).filter(o => activeSet.has(o.type)).length;
});

watch(filteredAndSortedObjects, (newList) => {
    renderedLimit.value = 50;
    if (!props.selectedId && newList.length > 0) {
        handleObjectClick(newList[0]);
    }
}, { immediate: true });

const onScroll = (e) => {
    const { scrollTop, scrollHeight, clientHeight } = e.target;
    if (scrollTop + clientHeight >= scrollHeight - 300) {
        if (renderedLimit.value < filteredAndSortedObjects.value.length) {
            renderedLimit.value += 50;
        }
    }
};

const handleObjectClick = async (item) => {
    emit('select', item);
    if (!item) return;

    if (!item.altitude_graph) {
        try {
            const res = await fetch(`/api/object-details/${encodeURIComponent(item.name)}`);
            if (res.ok) {
                const details = await res.json();
                Object.assign(item, details);
            }
        } catch (e) {
            console.error("Failed to fetch object details on demand:", e);
        }
    }
};

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

const handleKeydown = (e) => {
    const list = filteredAndSortedObjects.value;
    if (list.length === 0) return;
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;

    if (e.key === 'ArrowDown') {
        const idx = list.findIndex(o => o.name === props.selectedId);
        if (idx < list.length - 1) handleObjectClick(list[idx + 1]);
        else if (idx === -1) handleObjectClick(list[0]);
        e.preventDefault();
    } else if (e.key === 'ArrowUp') {
        const idx = list.findIndex(o => o.name === props.selectedId);
        if (idx > 0) handleObjectClick(list[idx - 1]);
        e.preventDefault();
    }
};

onMounted(() => {
    window.addEventListener('keydown', handleKeydown);
});

onUnmounted(() => {
    window.removeEventListener('keydown', handleKeydown);
});
</script>

<template>
    <div class="list-wrapper">
        <div class="list-content">
            <!-- Unified Search + Sort + Count Header -->
            <div class="list-header">
                <div class="search-box">
                    <span class="search-icon">🔍</span>
                    <input
                        type="text"
                        :value="searchQuery"
                        @input="onSearchInput"
                        @focus="showSuggestions = true"
                        @blur="onSearchBlur"
                        placeholder="Search targets (name, M31, NGC...)"
                        class="search-input"
                    />
                    <button
                        v-if="searchQuery"
                        type="button"
                        class="clear-search-btn"
                        @click="clearSearch"
                        title="Clear search"
                    >
                        ✕
                    </button>

                    <div v-if="showSuggestions && suggestionList.length > 0" class="suggestions-popover">
                        <div
                            v-for="s in suggestionList"
                            :key="s.name"
                            class="suggestion-item"
                            @mousedown.prevent="selectSuggestion(s)"
                            @touchstart.passive="selectSuggestion(s)"
                        >
                            <div class="sug-main">
                                <strong>{{ s.name }}</strong>
                                <span v-if="s.aliases" class="sug-aliases">({{ s.aliases }})</span>
                            </div>
                            <span v-if="s.common_name && s.common_name !== 'N/A'" class="sug-common">
                                {{ s.common_name }}
                            </span>
                        </div>
                    </div>
                </div>

                <div class="toolbar-row">
                    <div class="sort-control">
                        <label for="sort-select">Sort:</label>
                        <select id="sort-select" v-model="localSettings.sort_key" @change="updateSettings" class="sort-select">
                            <option v-for="opt in sortOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                        </select>
                    </div>

                    <div class="list-stats" title="Matching targets / Total targets in selected object types">
                        <strong>{{ filteredAndSortedObjects.length }}</strong> / {{ typePoolCount }}
                    </div>
                </div>
            </div>

            <!-- Scrollable Target Cards -->
            <div class="scrollable-list" @scroll="onScroll">
                <div
                    v-for="item in displayedObjects"
                    :key="item.name"
                    :id="`obj-card-${item.name}`"
                    class="object-card"
                    :class="{ active: selectedId === item.name, 'search-match': item._isSearchMatch }"
                    @click="handleObjectClick(item)"
                >
                    <div class="card-content">
                        <div class="img-col">
                            <img v-if="item.image_url" :src="item.image_url" class="list-thumb" loading="lazy" />
                            <div v-else class="img-placeholder">
                                <span>✦</span>
                            </div>
                        </div>

                        <div class="info-col">
                            <div class="title-row">
                                <h4 :title="item.name">{{ item.name }}</h4>
                                <span
                                    v-if="item.type"
                                    class="type-tag"
                                    :title="`${getObjectTypeLabel(item.type)} (${item.type}) — ${getObjectTypeDescription(item.type)}`"
                                >
                                    {{ getObjectTypeShort(item.type) }}
                                </span>
                                <button type="button" class="info-icon" @click.stop="openInfo(item)" title="Target Details">ⓘ</button>

                                <div class="status-indicator" :class="`status-${item.status}`" :title="item.status">
                                    <span v-if="item.status === 'cached'">✓</span>
                                    <span v-else-if="item.status === 'downloading'">⟳</span>
                                    <span v-else-if="item.status === 'error'">✗</span>
                                    <span v-else>⋯</span>
                                </div>
                            </div>

                            <div class="common-name" :title="item.common_name && item.common_name !== 'N/A' ? item.common_name + ' • ' + item.constellation : item.constellation">
                                <template v-if="item.common_name && item.common_name !== 'N/A'">
                                    {{ item.common_name }} &bull;
                                </template>
                                {{ item.constellation }}
                            </div>

                            <div
                                v-if="item.aliases"
                                class="aliases-line"
                                :title="`Same-names / collated IDs: ${item.aliases}`"
                            >
                                ≡ {{ item.aliases }}
                            </div>

                            <div class="metrics-row">
                                <span>Mag: <strong>{{ formatMagnitude(item.mag) }}</strong></span>
                                <span>Size: <strong>{{ item.size }}</strong></span>
                                <span class="vis-time">Vis: {{ item._dynamicHours || 0 }}h</span>
                            </div>
                        </div>

                        <div class="graph-col">
                            <div class="altitude-chart" v-if="item.altitude_graph && item.altitude_graph.length">
                                <svg viewBox="0 0 100 40" preserveAspectRatio="none" width="100%" height="40">
                                    <line x1="0" y1="26" x2="100" y2="26" stroke="#374151" stroke-width="1" stroke-dasharray="2" />
                                    <path :d="getAltitudePath(item.altitude_graph)" fill="rgba(16, 185, 129, 0.25)" stroke="#10b981" stroke-width="1.2" />
                                </svg>
                            </div>
                            <div class="coord-label">
                                {{ typeof item.ra === 'number' ? item.ra.toFixed(1) : item.ra }}° / {{ typeof item.dec === 'number' ? item.dec.toFixed(1) : item.dec }}°
                            </div>
                        </div>
                    </div>
                </div>

                <div v-if="filteredAndSortedObjects.length === 0" class="empty-list">
                    <p>No targets match your current filters.</p>
                </div>
            </div>
        </div>

        <!-- Info Modal -->
        <div v-if="showInfoModal" class="modal-overlay" @click="showInfoModal = false">
            <div class="modal-content" @click.stop>
                <h3>{{ infoObject?.common_name && infoObject?.common_name !== 'N/A' ? infoObject.common_name : infoObject?.name }}</h3>
                <p v-if="infoObject?.common_name && infoObject?.common_name !== 'N/A'"><strong>Catalog Name:</strong> {{ infoObject.name }}</p>
                <p v-if="infoObject?.aliases"><strong>Also Known As:</strong> {{ infoObject.aliases }}</p>
                <p v-if="infoObject?.sub_objects && infoObject.sub_objects.length > 0">
                    <strong>Collated in Field ({{ infoObject.sub_objects.length }}):</strong>
                    {{ infoObject.sub_objects.map(s => s.name).join(', ') }}
                </p>
                <p><strong>RA/DEC:</strong> {{ typeof infoObject?.ra === 'number' ? infoObject.ra.toFixed(3) : infoObject?.ra }} / {{ typeof infoObject?.dec === 'number' ? infoObject.dec.toFixed(3) : infoObject?.dec }}</p>
                <p><strong>Type:</strong> {{ getObjectTypeLabel(infoObject?.type) }} <small v-if="infoObject?.type">({{ infoObject.type }})</small></p>
                <p><strong>Constellation:</strong> {{ infoObject?.constellation }}</p>
                <p><strong>Magnitude:</strong> {{ formatMagnitude(infoObject?.mag) }}</p>
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
    flex-direction: column;
    height: 100%;
    overflow: hidden;
    background: #111827;
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
    padding: 8px 10px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    z-index: 50;
}

.search-box {
    position: relative;
    display: flex;
    align-items: center;
    background: #111827;
    border: 1px solid #4b5563;
    border-radius: 6px;
    padding: 0 8px;
    height: 34px;
}

.search-box:focus-within {
    border-color: #10b981;
}

.search-icon {
    font-size: 0.8rem;
    margin-right: 6px;
    opacity: 0.7;
}

.search-input {
    flex: 1;
    background: transparent !important;
    border: none !important;
    color: #f3f4f6 !important;
    font-size: 0.85rem;
    padding: 0 !important;
    margin: 0 !important;
    height: 100% !important;
    outline: none !important;
    box-shadow: none !important;
}

.clear-search-btn {
    background: transparent;
    border: none;
    color: #9ca3af;
    cursor: pointer;
    font-size: 0.85rem;
    padding: 2px 4px;
    margin: 0;
}

.clear-search-btn:hover {
    color: #fff;
}

.suggestions-popover {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    right: 0;
    background: #1f2937;
    border: 1px solid #4b5563;
    border-radius: 6px;
    max-height: 260px;
    overflow-y: auto;
    z-index: 100;
    box-shadow: 0 8px 16px rgba(0, 0, 0, 0.5);
}

.suggestion-item {
    padding: 7px 10px;
    cursor: pointer;
    font-size: 0.82rem;
    border-bottom: 1px solid #374151;
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.suggestion-item:hover {
    background: #374151;
}

.sug-main {
    display: flex;
    align-items: baseline;
    gap: 5px;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.sug-aliases {
    color: #60a5fa;
    font-size: 0.72rem;
    overflow: hidden;
    text-overflow: ellipsis;
}

.sug-common {
    color: #9ca3af;
    font-size: 0.75rem;
    flex-shrink: 0;
    margin-left: 6px;
}

.toolbar-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
}

.sort-control {
    display: flex;
    align-items: center;
    gap: 6px;
}

.sort-control label {
    font-size: 0.75rem;
    color: #9ca3af;
    margin: 0;
}

.sort-select {
    background: #111827;
    color: #e5e7eb;
    border: 1px solid #4b5563;
    border-radius: 4px;
    padding: 3px 8px;
    font-size: 0.78rem;
    height: 26px;
    margin: 0;
}

.list-stats {
    font-size: 0.75rem;
    color: #9ca3af;
}

.list-stats strong {
    color: #10b981;
}

/* Scrollable List & Cards */
.scrollable-list {
    flex: 1;
    overflow-y: auto;
    padding: 6px;
}

.object-card {
    background: #111827;
    border: 1px solid #374151;
    margin-bottom: 6px;
    cursor: pointer;
    border-radius: 6px;
    overflow: hidden;
    transition: border-color 0.15s, background-color 0.15s;
}

.object-card:hover {
    background: #1f2937;
    border-color: #4b5563;
}

.object-card.active {
    border-color: #10b981;
    background: rgba(16, 185, 129, 0.08);
}

.object-card.search-match {
    border-left: 3px solid #60a5fa;
}

.card-content {
    display: flex;
    min-height: 76px;
    align-items: stretch;
}

.img-col {
    width: 68px;
    flex-shrink: 0;
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
    background: #0f172a;
    color: #334155;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 1.2rem;
}

.info-col {
    flex: 1;
    padding: 6px 8px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    gap: 2px;
    min-width: 0;
}

.title-row {
    display: flex;
    align-items: center;
    gap: 5px;
    min-width: 0;
}

.info-col h4 {
    margin: 0;
    font-size: 0.88rem;
    line-height: 1.2;
    font-weight: 700;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    color: #f3f4f6;
}

.type-tag {
    font-size: 0.63rem;
    line-height: 1.2;
    background: #374151;
    color: #d1d5db;
    padding: 1px 5px;
    border-radius: 3px;
    white-space: nowrap;
    flex-shrink: 0;
}

.info-icon {
    background: transparent;
    border: none;
    cursor: pointer;
    color: #60a5fa;
    font-size: 0.82rem;
    padding: 0 2px;
    margin: 0;
    line-height: 1;
    flex-shrink: 0;
}

.status-indicator {
    margin-left: auto;
    font-size: 0.8rem;
    font-weight: bold;
    line-height: 1;
    white-space: nowrap;
    flex-shrink: 0;
}

.status-cached { color: #10b981; }
.status-downloading { color: #f59e0b; }
.status-error { color: #ef4444; }

.common-name {
    font-size: 0.72rem;
    line-height: 1.25;
    color: #9ca3af;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.aliases-line {
    font-size: 0.66rem;
    line-height: 1.2;
    color: #60a5fa;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    opacity: 0.9;
}

.metrics-row {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 0.7rem;
    line-height: 1.2;
    color: #9ca3af;
    white-space: nowrap;
}

.metrics-row span {
    white-space: nowrap;
}

.metrics-row strong {
    color: #e5e7eb;
}

.vis-time {
    color: #10b981;
    font-weight: 600;
    margin-left: auto;
    white-space: nowrap;
}

.graph-col {
    width: 82px;
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 4px 5px;
    background: rgba(0, 0, 0, 0.35);
    border-left: 1px solid rgba(55, 65, 81, 0.5);
}

.altitude-chart {
    width: 100%;
    height: 38px;
}

.coord-label {
    font-size: 0.62rem;
    color: #6b7280;
    text-align: center;
    margin-top: 2px;
    white-space: nowrap;
}

.empty-list {
    text-align: center;
    color: #6b7280;
    padding: 30px 15px;
    font-size: 0.85rem;
}

/* Info Modal */
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
    z-index: 3000;
}

.modal-content {
    background: #1f2937;
    padding: 20px;
    border-radius: 8px;
    border: 1px solid #374151;
    max-width: 320px;
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

@media (max-width: 768px) {
    .search-box {
        height: 38px;
    }

    .search-input {
        font-size: 16px !important;
    }

    .sort-select {
        height: 30px;
        font-size: 0.82rem;
    }

    .info-icon {
        padding: 4px 6px;
        font-size: 0.95rem;
    }
}
</style>
