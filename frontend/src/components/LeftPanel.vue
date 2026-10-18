<script setup>
import { ref, onMounted, onUnmounted, watch, computed } from 'vue';
import { computeSensorFov, computeDefaultImageFov } from '../utils/fov.js';
import {
    getObjectTypeLabel,
    getObjectTypeIcon,
    getObjectTypeDescription,
    getCatalogMeta
} from '../utils/catalogMeta.js';

const props = defineProps({
    settings: Object,
    clientSettings: Object,
    availableTypes: Array,
    typeCounts: {
        type: Object,
        default: () => ({})
    },
    streamStatus: String,
    isDownloading: Boolean,
    downloadProgress: String,
    systemFov: Number,
    selectedObject: Object,
    totalObjectsCount: {
        type: Number,
        default: 0
    }
});

const emit = defineEmits([
    'update-settings',
    'update-client-settings',
    'start-stream',
    'stop-stream',
    'purge-cache',
    'download-filtered',
    'download-all',
    'stop-download'
]);

const activeTab = ref('filters'); // 'filters' | 'equipment' | 'location' | 'images' | 'nina' | null

const toggleTab = (tabName) => {
    activeTab.value = activeTab.value === tabName ? null : tabName;
};

const closePanel = () => {
    activeTab.value = null;
};

const localSettings = ref({});
const localClientSettings = ref({});
const profiles = ref({});
const presets = ref({ cameras: {} });
const cacheStatus = ref({ size_mb: 0, count: 0 });
const selectedProfileName = ref(null);
const newProfileName = ref("");
const availableCatalogs = ref([]);

// Data Fetching
const fetchProfiles = async () => {
    try {
        const res = await fetch('/api/profiles');
        if (res.ok) {
            profiles.value = await res.json();
            selectedProfileName.value = localSettings.value.active_profile || null;
        }
    } catch (e) { console.error(e); }
};

const fetchPresets = async () => {
    try {
        const res = await fetch('/api/presets');
        if (res.ok) presets.value = await res.json();
    } catch (e) { console.error(e); }
};

const fetchCacheStatus = async () => {
    try {
        const res = await fetch('/api/cache/status');
        if (res.ok) cacheStatus.value = await res.json();
    } catch (e) { console.error(e); }
};

const fetchCatalogs = async () => {
    try {
        const res = await fetch('/api/catalogs');
        if (res.ok) availableCatalogs.value = await res.json();
    } catch (e) { console.error(e); }
};

// Sync settings
watch(() => props.settings, (newVal) => {
    if (newVal) {
        localSettings.value = JSON.parse(JSON.stringify(newVal));
        if (localSettings.value.min_altitude === undefined) localSettings.value.min_altitude = 30.0;
        if (localSettings.value.nina_host === undefined) localSettings.value.nina_host = "";
        if (!localSettings.value.catalogs) localSettings.value.catalogs = ['messier'];
        if (localSettings.value.image_padding === undefined) localSettings.value.image_padding = 1.15;
        if (!localSettings.value.image_server) {
            localSettings.value.image_server = { resolution: 1024, timeout: 120, source: 'dss2r' };
        }
        selectedProfileName.value = localSettings.value.active_profile || null;
    }
}, { immediate: true, deep: true });

watch(() => props.clientSettings, (newVal) => {
    if (newVal) {
        localClientSettings.value = JSON.parse(JSON.stringify(newVal));
        if (localClientSettings.value.filter_active_alt === undefined) localClientSettings.value.filter_active_alt = true;
        if (localClientSettings.value.filter_na_alt === undefined) localClientSettings.value.filter_na_alt = true;
        if (localClientSettings.value.filter_active_hours === undefined) localClientSettings.value.filter_active_hours = true;
        if (localClientSettings.value.filter_na_hours === undefined) localClientSettings.value.filter_na_hours = true;
        if (localClientSettings.value.filter_active_mag === undefined) localClientSettings.value.filter_active_mag = true;
        if (localClientSettings.value.filter_na_mag === undefined) localClientSettings.value.filter_na_mag = true;
        if (localClientSettings.value.filter_active_size === undefined) localClientSettings.value.filter_active_size = true;
        if (localClientSettings.value.filter_na_size === undefined) localClientSettings.value.filter_na_size = true;
        if (!localClientSettings.value.selected_types) localClientSettings.value.selected_types = [];
    }
}, { immediate: true, deep: true });

onMounted(() => {
    fetchProfiles();
    fetchPresets();
    fetchCacheStatus();
    fetchCatalogs();
    // Collapse by default on narrow screens
    if (window.innerWidth <= 1024) {
        activeTab.value = null;
    }
    const interval = setInterval(fetchCacheStatus, 10000);
    onUnmounted(() => clearInterval(interval));
});

const emitSettings = () => {
    emit('update-settings', localSettings.value);
};

const emitClientSettings = () => {
    emit('update-client-settings', localClientSettings.value);
};

// Catalog & Type Helpers
const selectAllCatalogs = () => {
    localSettings.value.catalogs = [...availableCatalogs.value];
    emitSettings();
};

const clearCatalogs = () => {
    localSettings.value.catalogs = [];
    emitSettings();
};

const isAllTypesSelected = computed(() => {
    const sel = localClientSettings.value.selected_types || [];
    if (sel.length === 0) return true;
    if (sel.includes('__NONE__')) return false;
    return props.availableTypes?.length > 0 && sel.length >= props.availableTypes.length;
});

const isTypeActive = (type) => {
    const sel = localClientSettings.value.selected_types || [];
    if (sel.length === 0) return true;
    if (sel.includes('__NONE__')) return false;
    return sel.includes(type);
};

const selectAllTypes = () => {
    localClientSettings.value.selected_types = [];
    emitClientSettings();
};

const clearSelectedTypes = () => {
    localClientSettings.value.selected_types = ['__NONE__'];
    emitClientSettings();
};

const toggleType = (type) => {
    const allTypes = props.availableTypes || [];
    const current = localClientSettings.value.selected_types || [];

    if (current.length === 0) {
        // Currently "All" are active -> clicking one unchecks it (selects all except this one)
        const remaining = allTypes.filter(t => t !== type);
        localClientSettings.value.selected_types = remaining.length > 0 ? remaining : ['__NONE__'];
    } else if (current.includes('__NONE__')) {
        // Currently "None" are active -> clicking one selects just that type
        localClientSettings.value.selected_types = [type];
    } else {
        const idx = current.indexOf(type);
        let next = [...current];
        if (idx === -1) {
            next.push(type);
        } else {
            next.splice(idx, 1);
        }
        if (next.length === 0) {
            localClientSettings.value.selected_types = ['__NONE__'];
        } else if (allTypes.length > 0 && next.length >= allTypes.length) {
            localClientSettings.value.selected_types = [];
        } else {
            localClientSettings.value.selected_types = next;
        }
    }
    emitClientSettings();
};

const sortedAvailableTypes = computed(() => {
    if (!props.availableTypes) return [];
    return [...props.availableTypes].sort((a, b) => {
        const countDiff = (props.typeCounts?.[b] || 0) - (props.typeCounts?.[a] || 0);
        if (countDiff !== 0) return countDiff;
        return getObjectTypeLabel(a).localeCompare(getObjectTypeLabel(b));
    });
});

// Profile Actions
const loadProfile = (name) => {
    if (profiles.value[name]) {
        const profileData = JSON.parse(JSON.stringify(profiles.value[name]));
        const savedLocation = localSettings.value.location;
        const savedNina = localSettings.value.nina_host;

        localSettings.value = {
            ...localSettings.value,
            ...profileData,
            active_profile: name
        };

        if (savedLocation) localSettings.value.location = savedLocation;
        if (savedNina !== undefined) localSettings.value.nina_host = savedNina;

        selectedProfileName.value = name;
        emitSettings();
    }
};

const updateCurrentProfile = async () => {
    if (!selectedProfileName.value) return;
    const name = selectedProfileName.value;

    const profileData = JSON.parse(JSON.stringify(localSettings.value));
    delete profileData.location;
    delete profileData.nina_host;
    delete profileData.active_profile;
    delete profileData.profiles;
    delete profileData.client_settings;

    try {
        await fetch(`/api/profiles/${name}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(profileData)
        });
        await fetchProfiles();
        localSettings.value.profiles = JSON.parse(JSON.stringify(profiles.value));
        emitSettings();
    } catch (e) { console.error(e); }
};

const createProfile = async () => {
    if (!newProfileName.value.trim()) return;
    const name = newProfileName.value.trim();

    const profileData = JSON.parse(JSON.stringify(localSettings.value));
    delete profileData.location;
    delete profileData.nina_host;
    delete profileData.active_profile;
    delete profileData.profiles;
    delete profileData.client_settings;

    try {
        await fetch(`/api/profiles/${name}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(profileData)
        });
        await fetchProfiles();

        selectedProfileName.value = name;
        localSettings.value.active_profile = name;
        localSettings.value.profiles = JSON.parse(JSON.stringify(profiles.value));

        emitSettings();
        newProfileName.value = "";
    } catch (e) { console.error(e); }
};

const deleteProfile = async () => {
    if (!selectedProfileName.value) return;
    if (!confirm(`Delete profile "${selectedProfileName.value}"?`)) return;
    try {
        await fetch(`/api/profiles/${selectedProfileName.value}`, { method: 'DELETE' });
        await fetchProfiles();

        if (selectedProfileName.value === localSettings.value.active_profile) {
            localSettings.value.active_profile = null;
        }

        localSettings.value.profiles = JSON.parse(JSON.stringify(profiles.value));
        emitSettings();
        selectedProfileName.value = null;
    } catch (e) { console.error(e); }
};

const applyPreset = (cameraName) => {
    const cam = presets.value.cameras[cameraName];
    if (cam && localSettings.value.camera) {
        localSettings.value.camera.sensor_width = cam.width;
        localSettings.value.camera.sensor_height = cam.height;
        emitSettings();
    }
};

const selectedCameraPreset = computed({
    get() {
        if (!localSettings.value.camera || !presets.value.cameras) return "";
        const w = localSettings.value.camera.sensor_width;
        const h = localSettings.value.camera.sensor_height;
        for (const [name, specs] of Object.entries(presets.value.cameras)) {
            if (Math.abs(specs.width - w) < 0.01 && Math.abs(specs.height - h) < 0.01) {
                return name;
            }
        }
        return "";
    },
    set(val) {
        applyPreset(val);
    }
});

const purgeCache = async () => {
    if (!confirm("Are you sure you want to purge the cache? This will delete all downloaded images.")) return;
    await fetch('/api/cache/purge', { method: 'POST' });
    await fetchCacheStatus();
    emit('purge-cache');
};

// City Search
const cityQuery = ref("");
const cityResults = ref([]);
const isSearchingCity = ref(false);

const searchCity = async () => {
    if (!cityQuery.value || cityQuery.value.length < 2) return;
    isSearchingCity.value = true;
    try {
        const res = await fetch(`/api/geocode?city=${encodeURIComponent(cityQuery.value)}`);
        if (res.ok) {
            cityResults.value = await res.json();
        }
    } catch (e) { console.error(e); }
    finally { isSearchingCity.value = false; }
};

const selectCity = (city) => {
    if (!localSettings.value.location) localSettings.value.location = {};
    localSettings.value.location.latitude = city.latitude;
    localSettings.value.location.longitude = city.longitude;
    localSettings.value.location.city_name = city.name;
    emitSettings();
    cityResults.value = [];
    cityQuery.value = "";
};

const fovDisplay = computed(() => {
    const tel = localSettings.value?.telescope;
    const cam = localSettings.value?.camera;
    const pad = localSettings.value?.image_padding;
    const sensor = computeSensorFov(tel, cam);
    if (sensor.w > 0 && sensor.h > 0) {
        const imgFov = computeDefaultImageFov(tel, cam, pad);
        return `${sensor.w.toFixed(2)}° × ${sensor.h.toFixed(2)}° (Image: ${imgFov.toFixed(2)}°)`;
    }
    if (props.systemFov) return `${props.systemFov.toFixed(2)}°`;
    return "-";
});

const locationDisplay = computed(() => {
    const loc = localSettings.value.location;
    if (!loc) return "Not set";
    if (loc.city_name) return loc.city_name;
    if (loc.latitude !== undefined && loc.longitude !== undefined) {
        return `${loc.latitude.toFixed(2)}°, ${loc.longitude.toFixed(2)}°`;
    }
    return "Not set";
});

const activeFiltersCount = computed(() => {
    let count = 0;
    if (localClientSettings.value.filter_active_alt) count++;
    if (localClientSettings.value.filter_active_hours && localClientSettings.value.min_hours > 0) count++;
    if (localClientSettings.value.filter_active_mag) count++;
    if (localClientSettings.value.filter_active_size && localClientSettings.value.min_size > 0) count++;
    if (localClientSettings.value.selected_types?.length > 0) count++;
    return count;
});
</script>

<template>
    <aside class="left-panel-wrapper">
        <!-- Mobile backdrop when drawer is open -->
        <div class="mobile-backdrop" v-if="activeTab" @click="closePanel"></div>

        <!-- Narrow Activity Rail -->
        <nav class="activity-rail" aria-label="Main Configuration Rail">
            <div class="rail-top">
                <button
                    type="button"
                    class="rail-btn"
                    :class="{ active: activeTab === 'filters' }"
                    @click="toggleTab('filters')"
                    title="Filters & Catalogs"
                    aria-label="Filters & Catalogs"
                >
                    <span class="rail-icon">⚡</span>
                    <span class="rail-label">Filters</span>
                    <span v-if="activeFiltersCount > 0" class="rail-badge">{{ activeFiltersCount }}</span>
                </button>

                <button
                    type="button"
                    class="rail-btn"
                    :class="{ active: activeTab === 'equipment' }"
                    @click="toggleTab('equipment')"
                    title="Equipment & Profiles"
                    aria-label="Equipment & Profiles"
                >
                    <span class="rail-icon">🔭</span>
                    <span class="rail-label">Rig</span>
                </button>

                <button
                    type="button"
                    class="rail-btn"
                    :class="{ active: activeTab === 'location' }"
                    @click="toggleTab('location')"
                    title="Observer Location"
                    aria-label="Observer Location"
                >
                    <span class="rail-icon">📍</span>
                    <span class="rail-label">Site</span>
                </button>

                <button
                    type="button"
                    class="rail-btn"
                    :class="{ active: activeTab === 'images' }"
                    @click="toggleTab('images')"
                    title="Image Server, Downloads & Cache"
                    aria-label="Image Server, Downloads & Cache"
                >
                    <span class="rail-icon">🖼️</span>
                    <span class="rail-label">Images</span>
                    <span v-if="isDownloading" class="rail-badge pulse">↓</span>
                </button>

                <button
                    type="button"
                    class="rail-btn"
                    :class="{ active: activeTab === 'nina' }"
                    @click="toggleTab('nina')"
                    title="N.I.N.A. Connection"
                    aria-label="N.I.N.A. Connection"
                >
                    <span class="rail-icon">📡</span>
                    <span class="rail-label">NINA</span>
                </button>
            </div>

            <div class="rail-bottom">
                <div class="fov-pill" :title="`System FOV: ${fovDisplay}`">
                    <span class="fov-lbl">FOV</span>
                    <span class="fov-val">{{ fovDisplay }}</span>
                </div>
                <div
                    class="status-dot"
                    :class="{
                        'online': streamStatus.includes('Complete') || streamStatus.includes('Loaded'),
                        'busy': isDownloading || streamStatus.includes('Connecting'),
                        'err': streamStatus.includes('Error')
                    }"
                    :title="`${streamStatus} (${totalObjectsCount} objects)`"
                ></div>
            </div>
        </nav>

        <!-- Expandable Drawer -->
        <div class="drawer-panel" v-if="activeTab">
            <header class="drawer-header">
                <h3>
                    <template v-if="activeTab === 'filters'">Filters & Catalogs</template>
                    <template v-else-if="activeTab === 'equipment'">Equipment & Profiles</template>
                    <template v-else-if="activeTab === 'location'">Observer Location</template>
                    <template v-else-if="activeTab === 'images'">Images & Cache</template>
                    <template v-else-if="activeTab === 'nina'">N.I.N.A. Integration</template>
                </h3>
                <button type="button" class="drawer-close" @click="closePanel" title="Collapse Panel">✕</button>
            </header>

            <div class="drawer-body">
                <!-- TAB 1: FILTERS & CATALOGS -->
                <div v-if="activeTab === 'filters'" class="tab-section">
                    <!-- Target Thresholds -->
                    <div class="section-card">
                        <div class="section-title-row">
                            <h4>Visibility & Target Thresholds</h4>
                        </div>

                        <!-- Max Altitude -->
                        <div class="threshold-item">
                            <div class="threshold-top">
                                <label class="toggle-label">
                                    <input
                                        type="checkbox"
                                        v-model="localClientSettings.filter_active_alt"
                                        @change="emitClientSettings"
                                    />
                                    <span>Min Peak Altitude (°)</span>
                                </label>
                                <input
                                    type="number"
                                    v-model.number="localSettings.min_altitude"
                                    :disabled="!localClientSettings.filter_active_alt"
                                    @input="emitSettings"
                                    class="num-input"
                                />
                            </div>
                            <label class="na-toggle" v-if="localClientSettings.filter_active_alt">
                                <input
                                    type="checkbox"
                                    v-model="localClientSettings.filter_na_alt"
                                    @change="emitClientSettings"
                                />
                                <span>Include objects with unknown altitude</span>
                            </label>
                        </div>

                        <!-- Min Hours -->
                        <div class="threshold-item">
                            <div class="threshold-top">
                                <label class="toggle-label">
                                    <input
                                        type="checkbox"
                                        v-model="localClientSettings.filter_active_hours"
                                        @change="emitClientSettings"
                                    />
                                    <span>Min Visible Hours (h)</span>
                                </label>
                                <input
                                    type="number"
                                    step="0.5"
                                    min="0"
                                    v-model.number="localClientSettings.min_hours"
                                    :disabled="!localClientSettings.filter_active_hours"
                                    @change="emitClientSettings"
                                    class="num-input"
                                />
                            </div>
                            <label class="na-toggle" v-if="localClientSettings.filter_active_hours">
                                <input
                                    type="checkbox"
                                    v-model="localClientSettings.filter_na_hours"
                                    @change="emitClientSettings"
                                />
                                <span>Include objects with unknown hours</span>
                            </label>
                        </div>

                        <!-- Max Magnitude -->
                        <div class="threshold-item">
                            <div class="threshold-top">
                                <label class="toggle-label">
                                    <input
                                        type="checkbox"
                                        v-model="localClientSettings.filter_active_mag"
                                        @change="emitClientSettings"
                                    />
                                    <span>Max Magnitude (≤)</span>
                                </label>
                                <input
                                    type="number"
                                    step="0.5"
                                    v-model.number="localClientSettings.max_magnitude"
                                    :disabled="!localClientSettings.filter_active_mag"
                                    @change="emitClientSettings"
                                    class="num-input"
                                />
                            </div>
                            <label class="na-toggle" v-if="localClientSettings.filter_active_mag">
                                <input
                                    type="checkbox"
                                    v-model="localClientSettings.filter_na_mag"
                                    @change="emitClientSettings"
                                />
                                <span>Include unrated magnitude (N/A)</span>
                            </label>
                        </div>

                        <!-- Min Size -->
                        <div class="threshold-item">
                            <div class="threshold-top">
                                <label class="toggle-label">
                                    <input
                                        type="checkbox"
                                        v-model="localClientSettings.filter_active_size"
                                        @change="emitClientSettings"
                                    />
                                    <span>Min Size (arcmin ≥)</span>
                                </label>
                                <input
                                    type="number"
                                    step="1"
                                    min="0"
                                    v-model.number="localClientSettings.min_size"
                                    :disabled="!localClientSettings.filter_active_size"
                                    @change="emitClientSettings"
                                    class="num-input"
                                />
                            </div>
                            <label class="na-toggle" v-if="localClientSettings.filter_active_size">
                                <input
                                    type="checkbox"
                                    v-model="localClientSettings.filter_na_size"
                                    @change="emitClientSettings"
                                />
                                <span>Include unknown size (N/A)</span>
                            </label>
                        </div>
                    </div>

                    <!-- Object Types -->
                    <div class="section-card">
                        <div class="section-title-row">
                            <div class="title-with-sub">
                                <h4>Object Types</h4>
                                <span class="sub-badge">
                                    {{ isAllTypesSelected ? 'All' : (localClientSettings.selected_types?.includes('__NONE__') ? '0' : localClientSettings.selected_types?.length) }}
                                    / {{ sortedAvailableTypes.length }}
                                </span>
                            </div>
                            <div class="inline-actions">
                                <button type="button" class="text-btn" @click="selectAllTypes">All</button>
                                <button type="button" class="text-btn" @click="clearSelectedTypes">None</button>
                            </div>
                        </div>
                        <div v-if="sortedAvailableTypes.length > 0" class="type-list-grid">
                            <button
                                v-for="type in sortedAvailableTypes"
                                :key="type"
                                type="button"
                                class="type-chip"
                                :class="{ active: isTypeActive(type) }"
                                @click="toggleType(type)"
                                :title="`${getObjectTypeLabel(type)} (${type}) — ${getObjectTypeDescription(type)}`"
                            >
                                <span class="type-chip-icon">{{ getObjectTypeIcon(type) }}</span>
                                <span class="type-chip-label">{{ getObjectTypeLabel(type) }}</span>
                                <span class="type-chip-count">{{ typeCounts[type] || 0 }}</span>
                            </button>
                        </div>
                        <div v-else class="empty-hint">No object types loaded yet.</div>
                    </div>

                    <!-- Catalogs -->
                    <div class="section-card">
                        <div class="section-title-row">
                            <div class="title-with-sub">
                                <h4>Active Catalogs</h4>
                                <span class="sub-badge">
                                    {{ localSettings.catalogs?.length || 0 }} / {{ availableCatalogs.length }}
                                </span>
                            </div>
                            <div class="inline-actions">
                                <button type="button" class="text-btn" @click="selectAllCatalogs">All</button>
                                <button type="button" class="text-btn" @click="clearCatalogs">None</button>
                            </div>
                        </div>
                        <div class="catalog-grid">
                            <label
                                v-for="cat in availableCatalogs"
                                :key="cat"
                                class="cat-chip"
                                :class="{ active: localSettings.catalogs?.includes(cat) }"
                                :title="`${getCatalogMeta(cat).label} (${getCatalogMeta(cat).code}) — ${getCatalogMeta(cat).subtitle}`"
                            >
                                <input
                                    type="checkbox"
                                    :value="cat"
                                    v-model="localSettings.catalogs"
                                    @change="emitSettings"
                                    hidden
                                />
                                <div class="cat-chip-main">
                                    <span class="cat-chip-title">{{ getCatalogMeta(cat).label }}</span>
                                    <span class="cat-chip-code">{{ getCatalogMeta(cat).code }}</span>
                                </div>
                                <span class="cat-chip-sub">{{ getCatalogMeta(cat).subtitle }}</span>
                            </label>
                        </div>
                    </div>
                </div>

                <!-- TAB 2: EQUIPMENT & PROFILES -->
                <div v-else-if="activeTab === 'equipment'" class="tab-section">
                    <div class="section-card">
                        <div class="section-title-row">
                            <h4>Equipment Profile</h4>
                            <span class="badge-accent">{{ selectedProfileName || 'Custom' }}</span>
                        </div>

                        <div class="profile-list" v-if="Object.keys(profiles).length > 0">
                            <button
                                v-for="(p, name) in profiles"
                                :key="name"
                                type="button"
                                class="profile-item"
                                :class="{ active: selectedProfileName === name }"
                                @click="loadProfile(name)"
                            >
                                <span>{{ name }}</span>
                                <span v-if="selectedProfileName === name" class="check">✓</span>
                            </button>
                        </div>

                        <div class="field-row" style="margin-top: 10px;">
                            <input
                                type="text"
                                v-model="newProfileName"
                                placeholder="New profile name..."
                                @keyup.enter="createProfile"
                                class="text-input"
                            />
                            <button type="button" class="btn-primary sm" @click="createProfile">Save New</button>
                        </div>

                        <div v-if="selectedProfileName" class="btn-row" style="margin-top: 8px;">
                            <button type="button" class="btn-secondary sm flex-1" @click="updateCurrentProfile">
                                Update "{{ selectedProfileName }}"
                            </button>
                            <button type="button" class="btn-danger sm" @click="deleteProfile" title="Delete Profile">
                                🗑️
                            </button>
                        </div>
                    </div>

                    <div class="section-card">
                        <h4>Optics & Sensor</h4>
                        <div class="form-field" v-if="localSettings.telescope">
                            <label>Telescope Focal Length (mm)</label>
                            <input
                                type="number"
                                v-model.number="localSettings.telescope.focal_length"
                                @change="emitSettings"
                                class="text-input"
                            />
                        </div>

                        <div class="form-field">
                            <label>Camera Preset</label>
                            <select v-model="selectedCameraPreset" class="select-input">
                                <option value="" disabled>Custom / Select Preset...</option>
                                <option v-for="(specs, name) in presets.cameras" :key="name" :value="name">
                                    {{ name }} ({{ specs.width }}×{{ specs.height }}mm)
                                </option>
                            </select>
                        </div>

                        <div class="two-col" v-if="localSettings.camera">
                            <div class="form-field">
                                <label>Sensor Width (mm)</label>
                                <input
                                    type="number"
                                    step="0.1"
                                    v-model.number="localSettings.camera.sensor_width"
                                    @change="emitSettings"
                                    class="text-input"
                                />
                            </div>
                            <div class="form-field">
                                <label>Sensor Height (mm)</label>
                                <input
                                    type="number"
                                    step="0.1"
                                    v-model.number="localSettings.camera.sensor_height"
                                    @change="emitSettings"
                                    class="text-input"
                                />
                            </div>
                        </div>

                        <div class="form-field">
                            <label>Framing Padding (1.0 = Exact Sensor FOV)</label>
                            <input
                                type="number"
                                step="0.05"
                                min="0.5"
                                max="3.0"
                                v-model.number="localSettings.image_padding"
                                @change="emitSettings"
                                class="text-input"
                            />
                        </div>

                        <div class="info-banner">
                            <span>Calculated System FOV:</span>
                            <strong>{{ fovDisplay }}</strong>
                        </div>
                    </div>
                </div>

                <!-- TAB 3: LOCATION -->
                <div v-else-if="activeTab === 'location'" class="tab-section">
                    <div class="section-card">
                        <div class="section-title-row">
                            <h4>Current Site</h4>
                            <span class="badge-accent">{{ locationDisplay }}</span>
                        </div>

                        <div class="form-field">
                            <label>Search City / Observatory</label>
                            <div class="field-row">
                                <input
                                    type="text"
                                    v-model="cityQuery"
                                    @keyup.enter="searchCity"
                                    placeholder="Enter city name..."
                                    class="text-input"
                                />
                                <button type="button" class="btn-primary sm" @click="searchCity">
                                    {{ isSearchingCity ? '...' : 'Search' }}
                                </button>
                            </div>
                        </div>

                        <ul v-if="cityResults.length > 0" class="search-results-list">
                            <li v-for="(city, idx) in cityResults" :key="idx" @click="selectCity(city)">
                                <strong>{{ city.name }}</strong>, {{ city.country }}
                                <small>({{ city.latitude.toFixed(2) }}, {{ city.longitude.toFixed(2) }})</small>
                            </li>
                        </ul>

                        <div class="two-col" v-if="localSettings.location" style="margin-top: 12px;">
                            <div class="form-field">
                                <label>Latitude (°)</label>
                                <input
                                    type="number"
                                    step="0.0001"
                                    v-model.number="localSettings.location.latitude"
                                    @change="emitSettings"
                                    class="text-input"
                                />
                            </div>
                            <div class="form-field">
                                <label>Longitude (°)</label>
                                <input
                                    type="number"
                                    step="0.0001"
                                    v-model.number="localSettings.location.longitude"
                                    @change="emitSettings"
                                    class="text-input"
                                />
                            </div>
                        </div>
                    </div>
                </div>

                <!-- TAB 4: IMAGE SERVER, DOWNLOADS & CACHE -->
                <div v-else-if="activeTab === 'images'" class="tab-section">
                    <div class="section-card">
                        <h4>Batch Image Downloads</h4>
                        <p class="helper-text">
                            Pre-fetch sky survey images for offline use or rapid browsing.
                        </p>
                        <div v-if="!isDownloading" class="btn-col">
                            <button type="button" class="btn-primary" @click="$emit('download-filtered')">
                                ⬇️ Download Filtered Objects
                            </button>
                            <button type="button" class="btn-secondary" @click="$emit('download-all')">
                                ⬇️ Download Entire Catalog
                            </button>
                        </div>
                        <div v-else class="download-active-box">
                            <div class="dl-status">{{ downloadProgress || 'Downloading...' }}</div>
                            <button type="button" class="btn-danger" @click="$emit('stop-download')">
                                🛑 Stop Downloading
                            </button>
                        </div>
                    </div>

                    <div class="section-card" v-if="localSettings.image_server">
                        <h4>Sky Survey Settings</h4>
                        <div class="form-field">
                            <label>Survey Source</label>
                            <select v-model="localSettings.image_server.source" @change="emitSettings" class="select-input">
                                <option value="dss2r">DSS2 Red (Default)</option>
                                <option value="dss2b">DSS2 Blue</option>
                                <option value="dss2ir">DSS2 IR</option>
                            </select>
                        </div>

                        <div class="two-col">
                            <div class="form-field">
                                <label>Resolution</label>
                                <select v-model.number="localSettings.image_server.resolution" @change="emitSettings" class="select-input">
                                    <option :value="256">256 px</option>
                                    <option :value="512">512 px</option>
                                    <option :value="1024">1024 px</option>
                                    <option :value="2048">2048 px</option>
                                </select>
                            </div>
                            <div class="form-field">
                                <label>Timeout (sec)</label>
                                <input
                                    type="number"
                                    min="10"
                                    max="300"
                                    v-model.number="localSettings.image_server.timeout"
                                    @change="emitSettings"
                                    class="text-input"
                                />
                            </div>
                        </div>
                    </div>

                    <div class="section-card">
                        <div class="section-title-row">
                            <h4>Local Image Cache</h4>
                            <span class="badge-accent">{{ cacheStatus.size_mb }} MB</span>
                        </div>
                        <div class="info-banner" style="margin-bottom: 10px;">
                            <span>Cached Files:</span>
                            <strong>{{ cacheStatus.count }} images</strong>
                        </div>
                        <button type="button" class="btn-danger full-width" @click="purgeCache">
                            🗑️ Purge Image Cache
                        </button>
                    </div>
                </div>

                <!-- TAB 5: N.I.N.A. -->
                <div v-else-if="activeTab === 'nina'" class="tab-section">
                    <div class="section-card">
                        <h4>N.I.N.A. Framing Assistant</h4>
                        <div class="form-field">
                            <label>Host / IP Address</label>
                            <input
                                type="text"
                                v-model="localSettings.nina_host"
                                placeholder="localhost or 192.168.x.x"
                                @change="emitSettings"
                                class="text-input"
                            />
                        </div>
                        <p class="helper-text">
                            Use <code>host.docker.internal</code> if running inside Docker, or the LAN IP address of your imaging PC.<br /><br />
                            <strong>Note:</strong> Requires the <em>Advanced API</em> plugin enabled in N.I.N.A.
                        </p>
                    </div>
                </div>
            </div>

            <footer class="drawer-footer">
                <span class="footer-status">{{ streamStatus }}</span>
                <span class="footer-count">{{ totalObjectsCount }} targets</span>
            </footer>
        </div>
    </aside>
</template>

<style scoped>
.left-panel-wrapper {
    display: flex;
    height: 100%;
    z-index: 1500;
    flex-shrink: 0;
    position: relative;
}

/* Activity Rail */
.activity-rail {
    width: 56px;
    background: #0f172a;
    border-right: 1px solid #1e293b;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    align-items: center;
    padding: 8px 0;
    user-select: none;
    z-index: 1502;
}

.rail-top {
    display: flex;
    flex-direction: column;
    gap: 6px;
    width: 100%;
    align-items: center;
}

.rail-btn {
    position: relative;
    width: 46px;
    height: 48px;
    border-radius: 8px;
    background: transparent;
    border: 1px solid transparent;
    color: #94a3b8;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 2px;
    cursor: pointer;
    padding: 4px 2px;
    transition: all 0.15s ease;
}

.rail-btn:hover {
    background: #1e293b;
    color: #f8fafc;
}

.rail-btn.active {
    background: rgba(16, 185, 129, 0.15);
    border-color: rgba(16, 185, 129, 0.4);
    color: #10b981;
}

.rail-icon {
    font-size: 1.15rem;
    line-height: 1;
}

.rail-label {
    font-size: 0.62rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    text-transform: uppercase;
}

.rail-badge {
    position: absolute;
    top: 3px;
    right: 3px;
    background: #10b981;
    color: #064e3b;
    font-size: 0.6rem;
    font-weight: 700;
    min-width: 14px;
    height: 14px;
    border-radius: 7px;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0 3px;
}

.rail-badge.pulse {
    background: #f59e0b;
    color: #000;
    animation: pulse 1.2s infinite;
}

@keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
}

.rail-bottom {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 10px;
    padding-bottom: 4px;
}

.fov-pill {
    display: flex;
    flex-direction: column;
    align-items: center;
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 3px 4px;
    width: 46px;
    text-align: center;
}

.fov-lbl {
    font-size: 0.55rem;
    color: #64748b;
    text-transform: uppercase;
    font-weight: 700;
}

.fov-val {
    font-size: 0.68rem;
    color: #e2e8f0;
    font-weight: 600;
}

.status-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: #64748b;
}

.status-dot.online {
    background: #10b981;
    box-shadow: 0 0 6px rgba(16, 185, 129, 0.6);
}

.status-dot.busy {
    background: #f59e0b;
    box-shadow: 0 0 6px rgba(245, 158, 11, 0.6);
}

.status-dot.err {
    background: #ef4444;
}

/* Expandable Drawer */
.drawer-panel {
    width: 300px;
    background: #111827;
    border-right: 1px solid #374151;
    display: flex;
    flex-direction: column;
    height: 100%;
    z-index: 1501;
    box-shadow: 4px 0 12px rgba(0, 0, 0, 0.25);
}

.drawer-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin: 0 !important;
    padding: 10px 14px;
    background: #1f2937;
    border-bottom: 1px solid #374151;
}

.drawer-header h3 {
    margin: 0;
    font-size: 0.95rem;
    font-weight: 600;
    color: #f3f4f6;
}

.drawer-close {
    background: transparent;
    border: none;
    color: #9ca3af;
    cursor: pointer;
    font-size: 1rem;
    padding: 2px 6px;
    border-radius: 4px;
}

.drawer-close:hover {
    background: #374151;
    color: #fff;
}

.drawer-body {
    flex: 1;
    overflow-y: auto;
    padding: 12px;
}

.tab-section {
    display: flex;
    flex-direction: column;
    gap: 12px;
}

.section-card {
    background: #1f2937;
    border: 1px solid #374151;
    border-radius: 6px;
    padding: 10px 12px;
}

.section-card h4 {
    margin: 0 0 8px 0;
    font-size: 0.8rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: #9ca3af;
}

.section-title-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
}

.section-title-row h4 {
    margin: 0;
    white-space: nowrap;
}

/* Threshold items */
.threshold-item {
    padding: 6px 0;
    border-bottom: 1px solid rgba(55, 65, 81, 0.6);
}

.threshold-item:last-child {
    border-bottom: none;
    padding-bottom: 0;
}

.threshold-top {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
}

.toggle-label {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 0.82rem;
    color: #e5e7eb;
    cursor: pointer;
    margin: 0;
}

.toggle-label input[type="checkbox"] {
    accent-color: #10b981;
    width: 14px;
    height: 14px;
    margin: 0;
}

.na-toggle {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 0.72rem;
    color: #9ca3af;
    margin: 4px 0 0 20px;
    cursor: pointer;
}

.na-toggle input[type="checkbox"] {
    accent-color: #f59e0b;
    width: 12px;
    height: 12px;
    margin: 0;
}

.num-input {
    width: 68px;
    background: #111827;
    border: 1px solid #4b5563;
    color: #fff;
    border-radius: 4px;
    padding: 3px 6px;
    font-size: 0.82rem;
    text-align: center;
    margin: 0;
    height: 28px;
}

.num-input:disabled {
    opacity: 0.4;
    cursor: not-allowed;
}

/* Object Types & Catalogs */
.title-with-sub {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
}

.sub-badge {
    font-size: 0.68rem;
    background: #111827;
    border: 1px solid #374151;
    color: #9ca3af;
    padding: 1px 6px;
    border-radius: 999px;
    font-weight: 600;
    white-space: nowrap;
    flex-shrink: 0;
    line-height: 1.3;
}

.type-list-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 5px;
    width: 100%;
}

.type-chip {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    min-width: 0;
    box-sizing: border-box;
    background: #111827;
    border: 1px solid #374151;
    color: #9ca3af;
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 0.78rem;
    cursor: pointer;
    text-align: left;
    margin: 0;
    transition: all 0.15s;
}

.type-chip:hover {
    border-color: #6b7280;
    color: #f3f4f6;
}

.type-chip.active {
    background: rgba(16, 185, 129, 0.14);
    border-color: rgba(16, 185, 129, 0.55);
    color: #ecfdf5;
}

.type-chip-icon {
    font-size: 0.88rem;
    line-height: 1;
    width: 18px;
    text-align: center;
    flex-shrink: 0;
}

.type-chip-label {
    flex: 1;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.type-chip.active .type-chip-label {
    color: #10b981;
}

.type-chip-count {
    font-size: 0.68rem;
    background: rgba(31, 41, 55, 0.9);
    color: #9ca3af;
    padding: 1px 6px;
    border-radius: 999px;
    font-weight: 600;
    flex-shrink: 0;
}

.type-chip.active .type-chip-count {
    background: rgba(16, 185, 129, 0.22);
    color: #34d399;
}

.catalog-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 6px;
    width: 100%;
}

.cat-chip {
    display: flex;
    flex-direction: column;
    gap: 2px;
    width: 100%;
    min-width: 0;
    max-width: 100%;
    box-sizing: border-box;
    overflow: hidden;
    background: #111827;
    border: 1px solid #374151;
    color: #9ca3af;
    padding: 6px 10px;
    border-radius: 6px;
    cursor: pointer;
    text-align: left;
    user-select: none;
    transition: all 0.15s;
    margin: 0;
}

.cat-chip:hover {
    border-color: #6b7280;
    color: #e5e7eb;
}

.cat-chip.active {
    background: rgba(16, 185, 129, 0.14);
    border-color: rgba(16, 185, 129, 0.55);
    color: #ecfdf5;
}

.cat-chip-main {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 6px;
}

.cat-chip-title {
    font-size: 0.8rem;
    font-weight: 600;
    color: #e5e7eb;
}

.cat-chip.active .cat-chip-title {
    color: #10b981;
}

.cat-chip-code {
    font-size: 0.66rem;
    background: rgba(31, 41, 55, 0.9);
    color: #9ca3af;
    padding: 1px 6px;
    border-radius: 4px;
    font-weight: 700;
    text-transform: uppercase;
}

.cat-chip.active .cat-chip-code {
    background: rgba(16, 185, 129, 0.22);
    color: #34d399;
}

.cat-chip-sub {
    font-size: 0.68rem;
    color: #6b7280;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.cat-chip.active .cat-chip-sub {
    color: #9ca3af;
}

/* Forms & Inputs */
.form-field {
    margin-bottom: 10px;
}

.form-field:last-child {
    margin-bottom: 0;
}

.form-field label {
    display: block;
    font-size: 0.75rem;
    color: #9ca3af;
    margin-bottom: 4px;
}

.text-input,
.select-input {
    width: 100%;
    background: #111827;
    border: 1px solid #4b5563;
    color: #f3f4f6;
    border-radius: 4px;
    padding: 6px 8px;
    font-size: 0.82rem;
    margin: 0;
    height: 32px;
    box-sizing: border-box;
}

.two-col {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
}

.field-row {
    display: flex;
    gap: 6px;
    align-items: center;
}

.btn-row {
    display: flex;
    gap: 6px;
}

.btn-col {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.flex-1 {
    flex: 1;
}

.full-width {
    width: 100%;
}

.btn-primary,
.btn-secondary,
.btn-danger {
    border: none;
    border-radius: 4px;
    padding: 7px 10px;
    font-size: 0.8rem;
    font-weight: 600;
    cursor: pointer;
    margin: 0;
    transition: opacity 0.15s;
}

.btn-primary {
    background: #10b981;
    color: #064e3b;
}

.btn-secondary {
    background: #374151;
    color: #f3f4f6;
}

.btn-danger {
    background: #ef4444;
    color: #fff;
}

.btn-primary.sm,
.btn-secondary.sm,
.btn-danger.sm {
    padding: 5px 8px;
    font-size: 0.75rem;
    height: 32px;
    white-space: nowrap;
}

.text-btn {
    background: transparent;
    border: none;
    color: #60a5fa;
    font-size: 0.75rem;
    cursor: pointer;
    padding: 2px 4px;
    margin: 0;
}

.text-btn:hover {
    text-decoration: underline;
}

.inline-actions {
    display: flex;
    gap: 6px;
    flex-shrink: 0;
}

.badge-accent {
    background: rgba(16, 185, 129, 0.15);
    color: #10b981;
    font-size: 0.72rem;
    padding: 2px 6px;
    border-radius: 4px;
    font-weight: 600;
}

.profile-list {
    display: flex;
    flex-direction: column;
    gap: 4px;
    max-height: 150px;
    overflow-y: auto;
}

.profile-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: #111827;
    border: 1px solid #374151;
    color: #e5e7eb;
    padding: 6px 10px;
    border-radius: 4px;
    font-size: 0.8rem;
    cursor: pointer;
    text-align: left;
    margin: 0;
}

.profile-item:hover {
    border-color: #6b7280;
}

.profile-item.active {
    border-color: #10b981;
    background: rgba(16, 185, 129, 0.1);
}

.check {
    color: #10b981;
    font-weight: bold;
}

.info-banner {
    display: flex;
    justify-content: space-between;
    align-items: center;
    background: #111827;
    padding: 6px 10px;
    border-radius: 4px;
    font-size: 0.78rem;
    color: #9ca3af;
    margin-top: 8px;
}

.info-banner strong {
    color: #10b981;
}

.search-results-list {
    list-style: none;
    padding: 0;
    margin: 6px 0 0 0;
    max-height: 150px;
    overflow-y: auto;
    border: 1px solid #374151;
    border-radius: 4px;
    background: #111827;
}

.search-results-list li {
    padding: 6px 8px;
    font-size: 0.78rem;
    cursor: pointer;
    border-bottom: 1px solid #1f2937;
    display: flex;
    flex-direction: column;
}

.search-results-list li:hover {
    background: #374151;
}

.helper-text {
    font-size: 0.75rem;
    color: #9ca3af;
    line-height: 1.4;
    margin: 0 0 10px 0;
}

.download-active-box {
    display: flex;
    flex-direction: column;
    gap: 8px;
    background: #111827;
    padding: 8px;
    border-radius: 4px;
    border: 1px solid #f59e0b;
}

.dl-status {
    font-size: 0.8rem;
    color: #fbbf24;
    text-align: center;
    font-weight: 600;
}

.empty-hint {
    font-size: 0.78rem;
    color: #6b7280;
    text-align: center;
    padding: 8px 0;
}

.drawer-footer {
    padding: 8px 12px;
    background: #0f172a;
    border-top: 1px solid #1e293b;
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 0.72rem;
    color: #94a3b8;
}

.mobile-backdrop {
    display: none;
}

@media (max-width: 1024px) {
    .drawer-panel {
        position: fixed;
        top: 0;
        left: 56px;
        bottom: 0;
        width: calc(100vw - 56px);
        max-width: 320px;
        z-index: 2500;
    }

    .mobile-backdrop {
        display: block;
        position: fixed;
        top: 0;
        left: 56px;
        right: 0;
        bottom: 0;
        background: rgba(0, 0, 0, 0.6);
        z-index: 2400;
    }
}
</style>
