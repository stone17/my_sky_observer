<script setup>
import { ref, watch, onMounted, onUnmounted } from 'vue';
import {
    isEphemerisModalOpen,
    closeEphemerisModal,
    savedEphemerides,
    showEphemeris,
    ensureEphemeridesLoaded,
    upsertSavedEphemeris,
    updateEphemerisItem,
    deleteEphemerisItem,
    interpolateEphemerisAtTime,
    locateInSkyView
} from '../utils/skyViewOptions.js';
import { formatDateInputYmd, parseObserverLocation } from '../utils/observingTime.js';

const props = defineProps({
    settings: { type: Object, default: () => ({}) },
    effectiveTimeMs: { type: Number, default: () => Date.now() }
});

const QUICK_SUGGESTIONS = [
    { name: 'C/2023 A3 (Tsuchinshan-ATLAS)', command: 'DES=C/2023 A3;CAP', type: 'Comet' },
    { name: '12P/Pons-Brooks', command: '12P;CAP', type: 'Comet' },
    { name: '1 Ceres', command: '1;', type: 'Dwarf Planet' },
    { name: '4 Vesta', command: '4;', type: 'Asteroid' },
    { name: '433 Eros', command: '433;', type: 'NEO Asteroid' },
    { name: '99942 Apophis', command: '99942;', type: 'NEO Asteroid' }
];

const searchQuery = ref('');
const searchResults = ref([]);
const isSearching = ref(false);
const isDownloading = ref(false);
const errorMessage = ref('');
const successMessage = ref('');

const form = ref({
    object_name: '',
    command: '',
    start_time: '',
    stop_time: '',
    step_hours: 12,
    color: '#10B981'
});

const resetDatesAroundActiveTime = () => {
    const baseMs = Number.isFinite(props.effectiveTimeMs) ? props.effectiveTimeMs : Date.now();
    const startMs = baseMs - 3 * 86400000;
    const stopMs = baseMs + 30 * 86400000;
    form.value.start_time = formatDateInputYmd(startMs);
    form.value.stop_time = formatDateInputYmd(stopMs);
};

watch(
    isEphemerisModalOpen,
    (open) => {
        if (open) {
            errorMessage.value = '';
            successMessage.value = '';
            ensureEphemeridesLoaded();
            if (!form.value.start_time || !form.value.stop_time) {
                resetDatesAroundActiveTime();
            }
        }
    },
    { immediate: true }
);

const selectTarget = (item) => {
    errorMessage.value = '';
    successMessage.value = '';
    form.value.object_name = item.name;
    form.value.command = item.command;
    if (!form.value.start_time || !form.value.stop_time) {
        resetDatesAroundActiveTime();
    }
};

const runSearch = async () => {
    const q = searchQuery.value.trim();
    if (q.length < 2) return;
    errorMessage.value = '';
    successMessage.value = '';
    isSearching.value = true;
    try {
        const resp = await fetch(`/api/ephemeris/search?q=${encodeURIComponent(q)}`);
        if (!resp.ok) {
            throw new Error(`Search failed (${resp.status})`);
        }
        const matches = await resp.json();
        searchResults.value = Array.isArray(matches) ? matches : [];
        if (searchResults.value.length === 1) {
            selectTarget(searchResults.value[0]);
        }
    } catch (err) {
        errorMessage.value = err.message || 'Failed to query JPL Horizons Lookup.';
    } finally {
        isSearching.value = false;
    }
};

const downloadEphemeris = async () => {
    if (!form.value.command && !form.value.object_name) return;
    errorMessage.value = '';
    successMessage.value = '';
    isDownloading.value = true;

    try {
        const loc = parseObserverLocation(props.settings);
        const payload = {
            object_name: form.value.object_name,
            command: form.value.command,
            start_time: form.value.start_time,
            stop_time: form.value.stop_time,
            step_hours: form.value.step_hours,
            color: form.value.color
        };
        if (loc) {
            payload.lat = loc.lat;
            payload.lon = loc.lon;
            payload.elevation = Number(props.settings?.location?.elevation) || 0.0;
        }

        const resp = await fetch('/api/ephemeris/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (!resp.ok) {
            const errJson = await resp.json().catch(() => ({}));
            throw new Error(errJson.detail || `Download failed (${resp.status})`);
        }
        const saved = await resp.json();
        const prepared = upsertSavedEphemeris(saved);
        showEphemeris.value = true;
        searchResults.value = [];
        successMessage.value = `Downloaded "${prepared?.name || saved.name}" (${saved.points?.length || 0} points).`;
    } catch (err) {
        errorMessage.value = err.message || 'Failed to download ephemeris from JPL Horizons.';
    } finally {
        isDownloading.value = false;
    }
};

const handleToggleVisible = (ep) => {
    updateEphemerisItem(ep.id, { visible: !ep.visible });
};

const handleColorChange = (ep, evt) => {
    const nextColor = evt.target?.value;
    if (nextColor) {
        updateEphemerisItem(ep.id, { color: nextColor });
    }
};

const handleDelete = async (ep) => {
    errorMessage.value = '';
    try {
        await deleteEphemerisItem(ep.id);
    } catch (err) {
        errorMessage.value = err.message || 'Failed to delete ephemeris.';
    }
};

const handleLocate = (ep) => {
    const baseMs = Number.isFinite(props.effectiveTimeMs) ? props.effectiveTimeMs : Date.now();
    const pos = interpolateEphemerisAtTime(ep, baseMs, {});
    if (!pos) return;
    if (!ep.visible) {
        updateEphemerisItem(ep.id, { visible: true });
    }
    showEphemeris.value = true;
    locateInSkyView(pos.ra, pos.dec, ep.name);
    closeEphemerisModal();
};

const formatSpanLabel = (ep) => {
    const a = ep.start_time ? ep.start_time.slice(0, 10) : '?';
    const b = ep.stop_time ? ep.stop_time.slice(0, 10) : '?';
    const count = ep.preparedPoints?.length || ep.points?.length || 0;
    return `${a} → ${b} · ${ep.step_hours}h step (${count} pts)`;
};

const onKeyDown = (e) => {
    if (e.key === 'Escape' && isEphemerisModalOpen.value) {
        closeEphemerisModal();
    }
};

onMounted(() => {
    window.addEventListener('keydown', onKeyDown);
});

onUnmounted(() => {
    window.removeEventListener('keydown', onKeyDown);
});
</script>

<template>
    <transition name="ephem-modal">
        <div
            v-if="isEphemerisModalOpen"
            class="ephem-modal-backdrop"
            @click.self="closeEphemerisModal"
        >
            <div class="ephem-modal-card" role="dialog" aria-modal="true" aria-label="JPL Horizons Ephemeris Manager">
                <div class="ephem-header">
                    <div class="ephem-title-wrap">
                        <span class="ephem-icon">☄</span>
                        <div>
                            <h3>Solar System & Minor-Body Ephemerides</h3>
                            <p class="ephem-subtitle">Search NASA JPL Horizons & overlay trajectories on the Sky Map</p>
                        </div>
                    </div>
                    <button type="button" class="ephem-close-btn" @click="closeEphemerisModal" title="Close (Esc)">✕</button>
                </div>

                <div class="ephem-body">
                    <div v-if="errorMessage" class="ephem-banner error">
                        <span>⚠ {{ errorMessage }}</span>
                        <button type="button" class="banner-dismiss" @click="errorMessage = ''">✕</button>
                    </div>
                    <div v-if="successMessage" class="ephem-banner success">
                        <span>✓ {{ successMessage }}</span>
                        <button type="button" class="banner-dismiss" @click="successMessage = ''">✕</button>
                    </div>

                    <!-- Search NASA JPL Horizons -->
                    <div class="ephem-section">
                        <label class="field-label">Search Comet, Asteroid, Planet, or Spacecraft</label>
                        <div class="search-row">
                            <input
                                type="text"
                                v-model="searchQuery"
                                placeholder="e.g. C/2023 A3, 12P, Halley, Ceres, Apophis, JWST..."
                                class="ephem-input"
                                @keyup.enter="runSearch"
                            />
                            <button
                                type="button"
                                class="ephem-search-btn"
                                :disabled="isSearching || searchQuery.trim().length < 2"
                                @click="runSearch"
                            >
                                {{ isSearching ? 'Searching…' : 'Search JPL' }}
                            </button>
                        </div>

                        <div class="quick-chips">
                            <span class="quick-label">Quick pick:</span>
                            <button
                                v-for="item in QUICK_SUGGESTIONS"
                                :key="item.command"
                                type="button"
                                class="quick-chip"
                                :class="{ active: form.command === item.command }"
                                @click="selectTarget(item)"
                            >
                                {{ item.name }}
                            </button>
                        </div>

                        <div v-if="searchResults.length > 0" class="results-list">
                            <button
                                v-for="res in searchResults"
                                :key="res.command"
                                type="button"
                                class="result-row"
                                :class="{ selected: form.command === res.command }"
                                @click="selectTarget(res)"
                            >
                                <span class="res-name">{{ res.name }}</span>
                                <span class="res-meta">{{ res.type }} · {{ res.command }}</span>
                            </button>
                        </div>
                    </div>

                    <!-- Download Trajectory Form -->
                    <div v-if="form.command" class="ephem-section form-box">
                        <div class="form-grid">
                            <div class="col-span-2">
                                <label class="field-label">Selected Target ({{ form.command }})</label>
                                <input type="text" v-model="form.object_name" class="ephem-input" />
                            </div>
                            <div>
                                <label class="field-label">Start Date (UTC)</label>
                                <input type="date" v-model="form.start_time" class="ephem-input" />
                            </div>
                            <div>
                                <label class="field-label">Stop Date (UTC)</label>
                                <input type="date" v-model="form.stop_time" class="ephem-input" />
                            </div>
                            <div>
                                <label class="field-label">Step Interval</label>
                                <select v-model.number="form.step_hours" class="ephem-select">
                                    <option :value="2">2 hours (Fast NEOs)</option>
                                    <option :value="6">6 hours</option>
                                    <option :value="12">12 hours (Recommended)</option>
                                    <option :value="24">24 hours (1 day)</option>
                                    <option :value="48">48 hours (2 days)</option>
                                </select>
                            </div>
                            <div>
                                <label class="field-label">Trail & Marker Color</label>
                                <div class="color-row">
                                    <input type="color" v-model="form.color" class="color-swatch-input" />
                                    <span class="color-hex">{{ form.color }}</span>
                                </div>
                            </div>
                        </div>

                        <button
                            type="button"
                            class="ephem-download-btn"
                            :disabled="isDownloading"
                            @click="downloadEphemeris"
                        >
                            {{ isDownloading ? 'Downloading from NASA JPL Horizons…' : '⬇ Download & Plot Trajectory' }}
                        </button>
                    </div>

                    <!-- Saved Trajectories -->
                    <div class="ephem-section">
                        <div class="saved-header">
                            <span class="field-label">Saved Trajectories ({{ savedEphemerides.length }})</span>
                        </div>

                        <div v-if="savedEphemerides.length === 0" class="empty-note">
                            No custom ephemerides downloaded yet. Pick a comet or asteroid above to plot its path.
                        </div>

                        <div v-else class="saved-list">
                            <div
                                v-for="ep in savedEphemerides"
                                :key="ep.id"
                                class="saved-row"
                                :class="{ muted: !ep.visible }"
                            >
                                <input
                                    type="color"
                                    :value="ep.color || '#10B981'"
                                    class="row-color-picker"
                                    title="Change trajectory color"
                                    @input="handleColorChange(ep, $event)"
                                />
                                <div class="saved-info">
                                    <div class="saved-name">{{ ep.name }}</div>
                                    <div class="saved-sub">{{ formatSpanLabel(ep) }}</div>
                                </div>
                                <div class="saved-actions">
                                    <button
                                        type="button"
                                        class="row-btn locate"
                                        title="Center Sky Map on object at current observing time"
                                        @click="handleLocate(ep)"
                                    >
                                        ⌖ Locate
                                    </button>
                                    <button
                                        type="button"
                                        class="row-btn"
                                        :class="{ active: ep.visible }"
                                        @click="handleToggleVisible(ep)"
                                    >
                                        {{ ep.visible ? 'Visible' : 'Hidden' }}
                                    </button>
                                    <button
                                        type="button"
                                        class="row-btn danger"
                                        title="Delete trajectory"
                                        @click="handleDelete(ep)"
                                    >
                                        ✕
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </transition>
</template>

<style scoped>
.ephem-modal-backdrop {
    position: fixed;
    inset: 0;
    z-index: 2200;
    background: rgba(3, 6, 14, 0.78);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 16px;
}

.ephem-modal-card {
    width: min(600px, 100%);
    max-height: 88vh;
    background: #0f172a;
    border: 1px solid rgba(99, 102, 241, 0.35);
    border-radius: 12px;
    box-shadow: 0 22px 54px rgba(0, 0, 0, 0.85);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    color: #e2e8f0;
}

.ephem-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 18px;
    background: rgba(15, 23, 42, 0.96);
    border-bottom: 1px solid rgba(148, 163, 184, 0.16);
}

.ephem-title-wrap {
    display: flex;
    align-items: center;
    gap: 11px;
}

.ephem-icon {
    font-size: 1.25rem;
    color: #38bdf8;
}

.ephem-title-wrap h3 {
    margin: 0;
    font-size: 0.98rem;
    font-weight: 600;
    color: #f8fafc;
}

.ephem-subtitle {
    margin: 2px 0 0;
    font-size: 0.74rem;
    color: #94a3b8;
}

.ephem-close-btn {
    background: transparent;
    border: 1px solid transparent;
    color: #94a3b8;
    width: 28px;
    height: 28px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.95rem;
}

.ephem-close-btn:hover {
    background: rgba(148, 163, 184, 0.14);
    color: #f8fafc;
}

.ephem-body {
    padding: 16px 18px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 16px;
}

.ephem-banner {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 10px;
    padding: 9px 12px;
    border-radius: 8px;
    font-size: 0.78rem;
    line-height: 1.4;
    white-space: pre-wrap;
}

.ephem-banner.error {
    background: rgba(239, 68, 68, 0.14);
    border: 1px solid rgba(239, 68, 68, 0.4);
    color: #fca5a5;
}

.ephem-banner.success {
    background: rgba(16, 185, 129, 0.14);
    border: 1px solid rgba(16, 185, 129, 0.4);
    color: #6ee7b7;
}

.banner-dismiss {
    background: transparent;
    border: none;
    color: inherit;
    cursor: pointer;
    opacity: 0.8;
}

.ephem-section {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.field-label {
    font-size: 0.76rem;
    font-weight: 600;
    color: #cbd5e1;
    letter-spacing: 0.01em;
}

.search-row {
    display: flex;
    gap: 8px;
}

.ephem-input,
.ephem-select {
    width: 100%;
    box-sizing: border-box;
    padding: 7px 10px;
    border-radius: 7px;
    border: 1px solid rgba(148, 163, 184, 0.25);
    background: #090e1a;
    color: #f1f5f9;
    font-size: 0.82rem;
}

.ephem-input:focus,
.ephem-select:focus {
    outline: none;
    border-color: #6366f1;
}

.ephem-search-btn {
    flex-shrink: 0;
    padding: 0 14px;
    border-radius: 7px;
    border: 1px solid rgba(99, 102, 241, 0.5);
    background: rgba(79, 70, 229, 0.25);
    color: #e0e7ff;
    font-size: 0.8rem;
    font-weight: 600;
    cursor: pointer;
}

.ephem-search-btn:hover:not(:disabled) {
    background: rgba(79, 70, 229, 0.42);
}

.ephem-search-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
}

.quick-chips {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 5px;
}

.quick-label {
    font-size: 0.72rem;
    color: #64748b;
    margin-right: 2px;
}

.quick-chip {
    padding: 3px 8px;
    border-radius: 999px;
    border: 1px solid rgba(148, 163, 184, 0.22);
    background: rgba(30, 41, 59, 0.65);
    color: #cbd5e1;
    font-size: 0.71rem;
    cursor: pointer;
}

.quick-chip:hover,
.quick-chip.active {
    border-color: rgba(56, 189, 248, 0.6);
    background: rgba(14, 116, 144, 0.28);
    color: #e0f2fe;
}

.results-list {
    max-height: 150px;
    overflow-y: auto;
    border: 1px solid rgba(148, 163, 184, 0.2);
    border-radius: 8px;
    background: #090e1a;
}

.result-row {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 7px 10px;
    background: transparent;
    border: none;
    border-bottom: 1px solid rgba(148, 163, 184, 0.1);
    color: #e2e8f0;
    text-align: left;
    cursor: pointer;
    font-size: 0.79rem;
}

.result-row:last-child {
    border-bottom: none;
}

.result-row:hover,
.result-row.selected {
    background: rgba(56, 189, 248, 0.14);
}

.res-name {
    font-weight: 600;
}

.res-meta {
    font-size: 0.71rem;
    color: #94a3b8;
    flex-shrink: 0;
}

.form-box {
    padding: 12px;
    border-radius: 9px;
    background: rgba(30, 41, 59, 0.45);
    border: 1px solid rgba(148, 163, 184, 0.18);
    gap: 11px;
}

.form-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
}

.col-span-2 {
    grid-column: span 2;
}

.color-row {
    display: flex;
    align-items: center;
    gap: 8px;
    height: 33px;
}

.color-swatch-input {
    width: 34px;
    height: 28px;
    padding: 0;
    border: 1px solid rgba(148, 163, 184, 0.3);
    border-radius: 5px;
    background: transparent;
    cursor: pointer;
}

.color-hex {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.76rem;
    color: #cbd5e1;
}

.ephem-download-btn {
    width: 100%;
    padding: 9px 14px;
    border-radius: 8px;
    border: 1px solid rgba(16, 185, 129, 0.55);
    background: rgba(5, 150, 105, 0.32);
    color: #ecfdf5;
    font-size: 0.82rem;
    font-weight: 600;
    cursor: pointer;
}

.ephem-download-btn:hover:not(:disabled) {
    background: rgba(5, 150, 105, 0.48);
}

.ephem-download-btn:disabled {
    opacity: 0.55;
    cursor: wait;
}

.empty-note {
    padding: 12px;
    border-radius: 8px;
    background: rgba(15, 23, 42, 0.6);
    border: 1px dashed rgba(148, 163, 184, 0.22);
    color: #94a3b8;
    font-size: 0.78rem;
    text-align: center;
}

.saved-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
}

.saved-row {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 10px;
    border-radius: 8px;
    background: rgba(30, 41, 59, 0.55);
    border: 1px solid rgba(148, 163, 184, 0.16);
}

.saved-row.muted {
    opacity: 0.58;
}

.row-color-picker {
    width: 22px;
    height: 22px;
    padding: 0;
    border: none;
    border-radius: 4px;
    background: transparent;
    cursor: pointer;
    flex-shrink: 0;
}

.saved-info {
    flex: 1;
    min-width: 0;
}

.saved-name {
    font-size: 0.81rem;
    font-weight: 600;
    color: #f1f5f9;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.saved-sub {
    font-size: 0.7rem;
    color: #94a3b8;
}

.saved-actions {
    display: flex;
    align-items: center;
    gap: 5px;
    flex-shrink: 0;
}

.row-btn {
    padding: 4px 8px;
    border-radius: 6px;
    border: 1px solid rgba(148, 163, 184, 0.25);
    background: rgba(15, 23, 42, 0.75);
    color: #cbd5e1;
    font-size: 0.72rem;
    cursor: pointer;
}

.row-btn.active {
    border-color: rgba(16, 185, 129, 0.45);
    color: #6ee7b7;
}

.row-btn.locate:hover {
    border-color: rgba(56, 189, 248, 0.6);
    color: #7dd3fc;
}

.row-btn.danger:hover {
    border-color: rgba(239, 68, 68, 0.55);
    color: #fca5a5;
}

.ephem-modal-enter-active,
.ephem-modal-leave-active {
    transition: opacity 0.15s ease;
}

.ephem-modal-enter-from,
.ephem-modal-leave-to {
    opacity: 0;
}
</style>
