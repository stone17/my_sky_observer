<script setup>
import { ref, onMounted, watch, computed } from 'vue';
import LeftPanel from './components/LeftPanel.vue';
import ObjectList from './components/ObjectList.vue';
import Framing from './components/Framing.vue';
import AltitudeGraph from './components/AltitudeGraph.vue';
import { computeDefaultImageFov, normalizeImageFov } from './utils/fov.js';

const settings = ref({});
const clientSettings = ref({
    max_magnitude: 12.0,
    min_size: 0.0,
    min_hours: 0.0,
    selected_types: [],
    filter_active_alt: true,
    filter_na_alt: true,
    filter_active_hours: true,
    filter_na_hours: true,
    filter_active_mag: true,
    filter_na_mag: true,
    filter_active_size: true,
    filter_na_size: true
});
const objects = ref([]);
const selectedObject = ref(null);
const streamStatus = ref('Idle');
const nightTimes = ref({});
const isDownloading = ref(false);
const downloadProgress = ref("");
const searchQuery = ref("");
const isLoading = ref(false);

// Mobile UI State
const showMobileList = ref(false);

// --- FOV STATE (Single Source of Truth) ---
const activeFov = ref(1.0);
const isManualFov = ref(false);

const systemFov = computed(() => {
    return computeDefaultImageFov(
        settings.value.telescope,
        settings.value.camera,
        settings.value.image_padding
    );
});

const equipmentSignature = computed(() => {
    const tel = settings.value?.telescope;
    const cam = settings.value?.camera;
    const pad = settings.value?.image_padding ?? 1.15;
    const srv = settings.value?.image_server;
    if (!tel || !cam) return '';
    return `${tel.focal_length}_${cam.sensor_width}_${cam.sensor_height}_${pad}_${srv?.resolution || 1024}_${srv?.source || 'dss2r'}`;
});

// --- CORE DOWNLOAD LOGIC ---
const downloadImage = async (obj, explicitFov = null) => {
    if (!obj) return;

    const targetName = obj.name;
    const requestedFov = normalizeImageFov(
        explicitFov !== null ? explicitFov : (isManualFov.value ? activeFov.value : systemFov.value),
        systemFov.value
    );

    obj.status = 'downloading';

    try {
        const res = await fetch('/api/download-object', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                object: {
                    name: obj.name,
                    ra: obj.ra,
                    dec: obj.dec
                },
                fov: requestedFov,
                settings: {
                    ...settings.value,
                    fov: requestedFov
                }
            })
        });

        if (res.ok) {
            const data = await res.json();
            const effectiveFov = data.image_fov || requestedFov;
            const applyResult = (targetObj) => {
                if (!targetObj) return;
                targetObj.image_url = data.url;
                targetObj.status = data.status || 'cached';
                targetObj.image_fov = effectiveFov;
                if (data.sensor_fov) targetObj.sensor_fov = data.sensor_fov;
                if (data.fov_rectangle) targetObj.fov_rectangle = data.fov_rectangle;
                if (data.setup_hash) targetObj.setup_hash = data.setup_hash;
            };

            applyResult(obj);
            const liveObj = objects.value.find(o => o.name === targetName);
            if (liveObj && liveObj !== obj) {
                applyResult(liveObj);
            }
            if (
                selectedObject.value &&
                selectedObject.value.name === targetName &&
                selectedObject.value !== obj &&
                selectedObject.value !== liveObj
            ) {
                applyResult(selectedObject.value);
            }
        } else {
            obj.status = 'error';
        }
    } catch (e) {
        console.error(e);
        obj.status = 'error';
    }
};

// Watcher 1: Equipment / Profile / Padding Changes
watch(equipmentSignature, (newSig, oldSig) => {
    if (!newSig) return;
    isManualFov.value = false;
    activeFov.value = systemFov.value;
    if (oldSig && newSig !== oldSig && selectedObject.value) {
        selectedObject.value.custom_framed = false;
        delete selectedObject.value.center_ra;
        delete selectedObject.value.center_dec;
        downloadImage(selectedObject.value, systemFov.value);
    }
});

// Watcher 2: Object Selection
watch(selectedObject, (newVal, oldVal) => {
    if (!newVal) return;
    if (!isManualFov.value) {
        activeFov.value = systemFov.value;
    }
    const targetFov = isManualFov.value ? activeFov.value : systemFov.value;
    const hasMatchingImage =
        Boolean(newVal.image_url) &&
        newVal.status === 'cached' &&
        Math.abs((newVal.image_fov || 0) - targetFov) < 0.01;

    if (!hasMatchingImage && newVal.status !== 'downloading') {
        downloadImage(newVal, targetFov);
    }
    if (newVal?.name !== oldVal?.name && window.innerWidth <= 1024) {
        showMobileList.value = false;
    }
});

// Watcher 3: Manual FOV Update from Framing HUD
const handleFovUpdate = (payload) => {
    const isObj = typeof payload === 'object' && payload !== null;
    const rawFov = isObj ? payload.fov : payload;
    const isReset = isObj ? Boolean(payload.isReset) : false;
    const skipDownload = isObj ? Boolean(payload.skipDownload) : false;

    if (isReset) {
        isManualFov.value = false;
        activeFov.value = systemFov.value;
        if (selectedObject.value) {
            selectedObject.value.custom_framed = false;
            delete selectedObject.value.center_ra;
            delete selectedObject.value.center_dec;
            if (!skipDownload) {
                downloadImage(selectedObject.value, systemFov.value);
            }
        }
        return;
    }

    const normalized = normalizeImageFov(rawFov, systemFov.value);
    isManualFov.value = Math.abs(normalized - systemFov.value) >= 0.01;
    activeFov.value = normalized;

    if (!skipDownload && selectedObject.value) {
        downloadImage(selectedObject.value, normalized);
    }
};

// --- STREAMING LOGIC ---
let eventSource = null;
let restartTimer = null;
let isInitializing = true;
let lastStreamLocationKey = '';
let objectMap = new Map();

const updateClientSettings = (newClientSettings) => {
    if (!newClientSettings) return;
    const cloned = JSON.parse(JSON.stringify(newClientSettings));
    Object.assign(clientSettings.value, cloned);
};

const fetchSettings = async () => {
    try {
        const res = await fetch('/api/settings');
        if (res.ok) {
            const data = await res.json();
            if (data.active_profile && data.profiles && data.profiles[data.active_profile]) {
                const profileData = JSON.parse(JSON.stringify(data.profiles[data.active_profile]));
                const savedLocation = data.location;
                const savedNina = data.nina_host;
                const savedCatalogs = data.catalogs;
                const savedProfiles = data.profiles;
                const activeProf = data.active_profile;

                Object.assign(data, profileData, {
                    active_profile: activeProf,
                    profiles: savedProfiles
                });
                if (savedLocation) data.location = savedLocation;
                if (savedNina !== undefined) data.nina_host = savedNina;
                if (savedCatalogs) data.catalogs = savedCatalogs;
            }
            settings.value = data;
            if (data.client_settings) {
                updateClientSettings(data.client_settings);
            }
        }
    } catch (e) { console.error(e); }
};

let saveTimeout = null;
const saveSettings = (newSettings) => {
    const payload = { ...settings.value, ...newSettings, client_settings: JSON.parse(JSON.stringify(clientSettings.value)) };
    settings.value = payload;
    if (saveTimeout) clearTimeout(saveTimeout);
    saveTimeout = setTimeout(async () => {
        try {
            await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
        } catch (e) { console.error(e); }
    }, 500);
};

const startStream = (mode = 'selected') => {
    if (restartTimer) {
        clearTimeout(restartTimer);
        restartTimer = null;
    }
    if (eventSource) eventSource.close();
    streamStatus.value = 'Connecting...';
    isLoading.value = true;

    if (mode === 'filtered' || mode === 'all') {
        isDownloading.value = true;
        downloadProgress.value = "Starting...";
    } else {
        isDownloading.value = false;
        downloadProgress.value = "";
    }

    const params = new URLSearchParams();
    if (settings.value.telescope) params.append('focal_length', settings.value.telescope.focal_length);
    if (settings.value.camera) {
        params.append('sensor_width', settings.value.camera.sensor_width);
        params.append('sensor_height', settings.value.camera.sensor_height);
    }
    const locKey = settings.value.location
        ? `${settings.value.location.latitude}_${settings.value.location.longitude}`
        : '';
    if (settings.value.location) {
        params.append('latitude', settings.value.location.latitude);
        params.append('longitude', settings.value.location.longitude);
    }
    if (settings.value.catalogs) params.append('catalogs', settings.value.catalogs.join(','));
    params.append('sort_key', settings.value.sort_key || 'time');
    params.append('min_altitude', settings.value.min_altitude ?? 30.0);
    params.append('image_padding', settings.value.image_padding || 1.15);
    const effectiveStreamFov = isManualFov.value ? activeFov.value : systemFov.value;
    if (effectiveStreamFov > 0) {
        params.append('fov', effectiveStreamFov);
    }

    if (settings.value.image_server) {
        params.append('image_resolution', settings.value.image_server.resolution || 1024);
        params.append('image_timeout', settings.value.image_server.timeout || 120);
        params.append('image_source', settings.value.image_server.source || 'dss2r');
    }

    params.append('download_mode', mode);
    if (clientSettings.value.max_magnitude !== undefined) params.append('max_magnitude', clientSettings.value.max_magnitude);
    if (clientSettings.value.min_size !== undefined) params.append('min_size', clientSettings.value.min_size);
    if (clientSettings.value.min_hours !== undefined) params.append('min_hours', clientSettings.value.min_hours);

    if (clientSettings.value.filter_active_alt !== undefined) params.append('filter_active_alt', clientSettings.value.filter_active_alt);
    if (clientSettings.value.filter_na_alt !== undefined) params.append('filter_na_alt', clientSettings.value.filter_na_alt);
    if (clientSettings.value.filter_active_hours !== undefined) params.append('filter_active_hours', clientSettings.value.filter_active_hours);
    if (clientSettings.value.filter_na_hours !== undefined) params.append('filter_na_hours', clientSettings.value.filter_na_hours);
    if (clientSettings.value.filter_active_mag !== undefined) params.append('filter_active_mag', clientSettings.value.filter_active_mag);
    if (clientSettings.value.filter_na_mag !== undefined) params.append('filter_na_mag', clientSettings.value.filter_na_mag);
    if (clientSettings.value.filter_active_size !== undefined) params.append('filter_active_size', clientSettings.value.filter_active_size);
    if (clientSettings.value.filter_na_size !== undefined) params.append('filter_na_size', clientSettings.value.filter_na_size);

    params.append('selected_types', (clientSettings.value.selected_types || []).join(','));

    eventSource = new EventSource(`/api/stream-objects?${params.toString()}`);

    eventSource.addEventListener('total', (e) => { streamStatus.value = `Found ${e.data} objects.`; });
    eventSource.addEventListener('download_progress', (e) => {
        try {
            const p = JSON.parse(e.data);
            if (p.current !== undefined) downloadProgress.value = `Downloading ${p.current} of ${p.total}`;
        } catch (err) { }
    });
    eventSource.addEventListener('night_times', (e) => { try { nightTimes.value = JSON.parse(e.data); } catch (e) { } });

    eventSource.addEventListener('catalog_metadata', (e) => {
        try {
            const newObjects = JSON.parse(e.data);
            const prevSelected = selectedObject.value;
            const currentId = prevSelected?.name;
            const sameLocation = locKey && locKey === lastStreamLocationKey;
            lastStreamLocationKey = locKey;

            const nextMap = new Map();
            const targetFov = isManualFov.value ? activeFov.value : systemFov.value;

            for (const item of newObjects) {
                const prevObj = objectMap.get(item.name);
                if (prevObj) {
                    if (sameLocation && prevObj.altitude_graph) {
                        item.altitude_graph = prevObj.altitude_graph;
                        item.moon_graph = prevObj.moon_graph;
                        item.sun_graph = prevObj.sun_graph;
                        if (prevObj.hours_above_min !== undefined) {
                            item.hours_above_min = prevObj.hours_above_min;
                        }
                    }
                    if (
                        prevObj.status === 'downloading' ||
                        (prevObj.image_url && Math.abs((prevObj.image_fov || 0) - targetFov) < 0.01)
                    ) {
                        item.image_url = prevObj.image_url;
                        item.image_fov = prevObj.image_fov;
                        item.status = prevObj.status;
                        item.center_ra = prevObj.center_ra;
                        item.center_dec = prevObj.center_dec;
                        item.custom_framed = prevObj.custom_framed;
                    }
                }
                nextMap.set(item.name, item);
            }

            objectMap = nextMap;
            objects.value = newObjects;

            if (currentId) {
                const found = objectMap.get(currentId);
                if (found) {
                    if (prevSelected && prevSelected.altitude_graph && !found.altitude_graph) {
                        found.altitude_graph = prevSelected.altitude_graph;
                        found.moon_graph = prevSelected.moon_graph;
                        found.sun_graph = prevSelected.sun_graph;
                    }
                    selectedObject.value = found;
                } else {
                    selectedObject.value = null;
                }
            }

            streamStatus.value = `Loaded ${newObjects.length} objects.`;
            isLoading.value = false;
        } catch (err) { }
    });

    eventSource.addEventListener('object_details', (e) => {
        try {
            const d = JSON.parse(e.data);
            const o = objectMap.get(d.name) || objects.value.find(obj => obj.name === d.name);
            if (o) {
                const targetFov = isManualFov.value ? activeFov.value : systemFov.value;
                const keepCurrentImage =
                    o.status === 'downloading' ||
                    (o.image_url && o.custom_framed) ||
                    (o.image_url && isManualFov.value && Math.abs((o.image_fov || 0) - targetFov) < 0.01) ||
                    (o.image_url && !d.image_url && Math.abs((o.image_fov || 0) - (d.image_fov || 0)) < 0.01);

                const savedUrl = o.image_url;
                const savedFov = o.image_fov;
                const savedStatus = o.status;
                const savedCenterRa = o.center_ra;
                const savedCenterDec = o.center_dec;
                const savedCustomFramed = o.custom_framed;

                Object.assign(o, d);

                if (keepCurrentImage) {
                    o.image_url = savedUrl;
                    o.image_fov = savedFov;
                    o.status = savedStatus;
                    if (savedCenterRa !== undefined) o.center_ra = savedCenterRa;
                    if (savedCenterDec !== undefined) o.center_dec = savedCenterDec;
                    if (savedCustomFramed !== undefined) o.custom_framed = savedCustomFramed;
                }

                if (selectedObject.value && selectedObject.value.name === o.name && selectedObject.value !== o) {
                    Object.assign(selectedObject.value, o);
                }
            }
        } catch (err) { }
    });

    eventSource.addEventListener('image_status', (e) => {
        const s = JSON.parse(e.data);
        const o = objectMap.get(s.name) || objects.value.find(obj => obj.name === s.name);
        if (o) {
            const isSelected = selectedObject.value && selectedObject.value.name === o.name;
            if (isSelected && isManualFov.value) {
                o.status = s.status;
            } else {
                o.status = s.status;
                if (s.url) o.image_url = s.url;
                if (s.image_fov) o.image_fov = s.image_fov;
            }
        }
    });

    eventSource.addEventListener('close', () => { streamStatus.value = 'Stream Complete'; isDownloading.value = false; isLoading.value = false; eventSource.close(); });
    eventSource.addEventListener('error', () => { streamStatus.value = 'Error/Disconnected'; isDownloading.value = false; isLoading.value = false; eventSource?.close(); });
};

const stopStream = () => {
    if (eventSource) { eventSource.close(); eventSource = null; streamStatus.value = 'Stopped'; isDownloading.value = false; isLoading.value = false; }
};

const handlePurge = () => { objectMap.clear(); objects.value = []; selectedObject.value = null; startStream('selected'); };

const availableTypes = computed(() => {
    const types = new Set();
    objects.value.forEach(o => { if (o.type) types.add(o.type); });
    return Array.from(types).sort();
});

const typeCounts = computed(() => {
    const counts = {};
    objects.value.forEach(o => {
        if (o.type) {
            counts[o.type] = (counts[o.type] || 0) + 1;
        }
    });
    return counts;
});

const streamParams = computed(() => ({
    loc: settings.value.location,
    tel: settings.value.telescope,
    cam: settings.value.camera,
    cats: (settings.value.catalogs || []).join(','),
    min_alt: settings.value.min_altitude,
    min_hrs: clientSettings.value.min_hours,
    sort: settings.value.sort_key,
    max_mag: clientSettings.value.max_magnitude,
    min_sz: clientSettings.value.min_size,
    fa_alt: clientSettings.value.filter_active_alt,
    fn_alt: clientSettings.value.filter_na_alt,
    fa_hrs: clientSettings.value.filter_active_hours,
    fn_hrs: clientSettings.value.filter_na_hours,
    fa_mag: clientSettings.value.filter_active_mag,
    fn_mag: clientSettings.value.filter_na_mag,
    fa_sz: clientSettings.value.filter_active_size,
    fn_sz: clientSettings.value.filter_na_size,
    types: (clientSettings.value.selected_types || []).join(','),
    pad: settings.value.image_padding,
    img_srv: settings.value.image_server,
}));

watch(streamParams, (newVal, oldVal) => {
    if (isInitializing || !oldVal) return;
    const changes = [];
    for (const key in newVal) {
        if (JSON.stringify(newVal[key]) !== JSON.stringify(oldVal[key])) changes.push(key);
    }
    if (changes.length > 0) {
        if (restartTimer) clearTimeout(restartTimer);
        restartTimer = setTimeout(() => { startStream('selected'); }, 1000);
    }
}, { deep: true });

onMounted(async () => {
    isInitializing = true;
    await fetchSettings();
    startStream('selected');
    isInitializing = false;
});

watch(clientSettings, () => {
    if (!isInitializing) saveSettings({});
}, { deep: true });
</script>

<template>
    <div class="app-container">
        <!-- Mobile Floating Action Button for Target List -->
        <div class="mobile-fabs">
            <button
                class="fab"
                type="button"
                @click="showMobileList = !showMobileList"
                title="Toggle Target List"
                aria-label="Toggle Target List"
            >
                📋
            </button>
        </div>

        <div class="main-layout">
            <!-- Left Expandable Panel (Activity Rail + Drawer) -->
            <LeftPanel
                :settings="settings"
                :client-settings="clientSettings"
                :available-types="availableTypes"
                :type-counts="typeCounts"
                :stream-status="streamStatus"
                :is-downloading="isDownloading"
                :download-progress="downloadProgress"
                :system-fov="systemFov"
                :selected-object="selectedObject"
                :total-objects-count="objects.length"
                @update-settings="saveSettings"
                @update-client-settings="updateClientSettings"
                @start-stream="startStream"
                @stop-stream="stopStream"
                @purge-cache="handlePurge"
                @download-filtered="startStream('filtered')"
                @download-all="startStream('all')"
                @stop-download="stopStream"
            />

            <!-- Center Framing Canvas -->
            <section class="framing-section">
                <div v-if="selectedObject" class="fill-height">
                    <Framing
                        :object="selectedObject"
                        :objects="objects"
                        :settings="settings"
                        :clientSettings="clientSettings"
                        :searchQuery="searchQuery"
                        :activeFov="activeFov"
                        @update-fov="handleFovUpdate"
                        @update-search="searchQuery = $event"
                        @select-object="selectedObject = $event"
                        @update-settings="saveSettings"
                        @update-client-settings="updateClientSettings"
                    />
                </div>
                <div v-else-if="isLoading" class="empty-state">
                    <div class="spinner"></div>
                    <h2>Loading Targets...</h2>
                    <p>Calculating visibility and altitudes, please wait.</p>
                </div>
                <div v-else class="empty-state">
                    <h2>Select an object to view</h2>
                    <p>Use the left panel to configure filters and equipment, or choose a target from the right sidebar.</p>
                </div>
            </section>

            <!-- Right Target Sidebar (Altitude Graph + Object List) -->
            <aside class="sidebar" :class="{ 'mobile-visible': showMobileList }">
                <div class="graph-panel">
                    <AltitudeGraph :object="selectedObject" :location="settings.location" :nightTimes="nightTimes" />
                </div>
                <div class="list-panel">
                    <ObjectList
                        :objects="objects"
                        :selectedId="selectedObject?.name"
                        :settings="settings"
                        :searchQuery="searchQuery"
                        :clientSettings="clientSettings"
                        :nightTimes="nightTimes"
                        @select="selectedObject = $event"
                        @update-search="searchQuery = $event"
                        @update-settings="saveSettings"
                        @update-client-settings="updateClientSettings"
                        @fetch-all="startStream('all')"
                    />
                </div>
            </aside>
        </div>
    </div>
</template>

<style>
:root {
    --bg-color: #111827;
    --text-color: #f3f4f6;
    --border-color: #374151;
}

html {
    background-color: var(--bg-color);
}

body {
    margin: 0;
    background-color: var(--bg-color);
    color: var(--text-color);
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    height: 100vh;
    overflow: hidden;
}

.app-container {
    display: flex;
    flex-direction: column;
    height: 100vh;
    width: 100vw;
    overflow: hidden;
}

.main-layout {
    display: flex;
    flex: 1;
    overflow: hidden;
    position: relative;
    height: 100%;
}

.framing-section {
    flex: 1;
    border-right: 1px solid var(--border-color);
    position: relative;
    min-width: 0;
    background: #000;
}

.fill-height {
    height: 100%;
}

.empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: #6b7280;
    text-align: center;
    padding: 20px;
}

.spinner {
    border: 4px solid rgba(255, 255, 255, 0.1);
    width: 40px;
    height: 40px;
    border-radius: 50%;
    border-left-color: #10b981;
    animation: spin 1s linear infinite;
    margin-bottom: 15px;
}

@keyframes spin {
    0% { transform: rotate(0deg); }
    100% { transform: rotate(360deg); }
}

.sidebar {
    width: 410px;
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
    background: var(--bg-color);
    z-index: 10;
}

.graph-panel {
    height: 190px;
    border-bottom: 1px solid var(--border-color);
    background: #000;
    flex-shrink: 0;
}

.list-panel {
    flex: 1;
    overflow: hidden;
}

.mobile-fabs {
    display: none;
}

/* Responsive adjustments for mobile and tablets */
@media (max-width: 1024px) {
    body {
        overflow: hidden;
    }

    .mobile-fabs {
        display: flex;
        position: fixed;
        bottom: 20px;
        right: 14px;
        flex-direction: column;
        gap: 10px;
        z-index: 3000;
    }

    .fab {
        width: 46px;
        height: 46px;
        border-radius: 50%;
        background: #10b981;
        color: white;
        border: none;
        box-shadow: 0 4px 10px rgba(0, 0, 0, 0.4);
        font-size: 1.2rem;
        display: flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
    }

    .framing-section {
        border-right: none;
        width: 100%;
        flex: 1 1 100%;
    }

    .sidebar {
        position: absolute !important;
        top: 0;
        right: 0;
        bottom: 0;
        width: 85%;
        max-width: 380px;
        transform: translateX(100%);
        transition: transform 0.3s ease;
        background: rgba(17, 24, 39, 0.98);
        box-shadow: -4px 0 12px rgba(0, 0, 0, 0.5);
        z-index: 2600;
    }

    .sidebar.mobile-visible {
        transform: translateX(0);
    }

    .graph-panel {
        height: 155px;
    }
}
</style>
