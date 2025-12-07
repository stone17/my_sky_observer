<script setup>
import { ref, onMounted, watch, onUnmounted, computed } from 'vue';
import TopBar from './components/TopBar.vue';
import ObjectList from './components/ObjectList.vue';
import Framing from './components/Framing.vue';
import AltitudeGraph from './components/AltitudeGraph.vue';
import TypeFilter from './components/TypeFilter.vue';

const settings = ref({});
const clientSettings = ref({
    max_magnitude: 12.0,
    min_size: 0.0,
    min_hours: 0.0,
    selected_types: []
});
const objects = ref([]);
const selectedObject = ref(null);
const streamStatus = ref('Idle');
const nightTimes = ref({});
const isDownloading = ref(false);
const downloadProgress = ref("");
const searchQuery = ref("");

// Stream handling
let eventSource = null;

const fetchSettings = async () => {
    try {
        const res = await fetch('/api/settings');
        if (res.ok) {
            const data = await res.json();
            settings.value = data;
            if (data.client_settings) {
                Object.assign(clientSettings.value, data.client_settings);
            }
        }
    } catch (e) { console.error(e); }
};

let saveTimeout = null;
const saveSettings = (newSettings) => {
    const payload = {
        ...settings.value,
        ...newSettings,
        client_settings: clientSettings.value
    };
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
    if (eventSource) eventSource.close();

    streamStatus.value = 'Connecting...';
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
    if (settings.value.location) {
        params.append('latitude', settings.value.location.latitude);
        params.append('longitude', settings.value.location.longitude);
    }
    if (settings.value.catalogs) params.append('catalogs', settings.value.catalogs.join(','));

    params.append('sort_key', settings.value.sort_key || 'time');
    params.append('min_altitude', settings.value.min_altitude || 30.0);
    params.append('image_padding', settings.value.image_padding || 1.05);

    if (settings.value.image_server) {
        params.append('image_resolution', settings.value.image_server.resolution || 512);
        params.append('image_timeout', settings.value.image_server.timeout || 60);
        params.append('image_source', settings.value.image_server.source || 'dss2r');
    }

    params.append('download_mode', mode);

    if (clientSettings.value.max_magnitude !== undefined) params.append('max_magnitude', clientSettings.value.max_magnitude);
    if (clientSettings.value.min_size !== undefined) params.append('min_size', clientSettings.value.min_size);
    if (clientSettings.value.min_hours !== undefined) params.append('min_hours', clientSettings.value.min_hours);

    if (clientSettings.value.selected_types && clientSettings.value.selected_types.length > 0) {
        params.append('selected_types', clientSettings.value.selected_types.join(','));
    }

    eventSource = new EventSource(`/api/stream-objects?${params.toString()}`);

    eventSource.addEventListener('total', (e) => {
        streamStatus.value = `Found ${e.data} objects.`;
    });

    eventSource.addEventListener('download_progress', (e) => {
        try {
            const progress = JSON.parse(e.data);
            if (progress.current !== undefined && progress.total !== undefined) {
                downloadProgress.value = `Downloading ${progress.current} of ${progress.total} images`;
            }
        } catch (err) { console.error("Error parsing progress", err); }
    });

    eventSource.addEventListener('night_times', (e) => {
        try {
            nightTimes.value = JSON.parse(e.data);
        } catch (e) { console.error("Error parsing night times", e); }
    });

    eventSource.addEventListener('catalog_metadata', (e) => {
        try {
            const newObjects = JSON.parse(e.data);
            const currentSelectedId = selectedObject.value?.name;
            objects.value = newObjects;

            if (currentSelectedId) {
                const found = objects.value.find(o => o.name === currentSelectedId);
                if (found) selectedObject.value = found;
            } else if (objects.value.length > 0) {
                selectedObject.value = objects.value[0];
            }

            streamStatus.value = `Loaded ${newObjects.length} objects. Fetching details...`;
        } catch (err) {
            console.error("Error parsing metadata", err);
        }
    });

    eventSource.addEventListener('object_details', (e) => {
        try {
            const detail = JSON.parse(e.data);
            const obj = objects.value.find(o => o.name === detail.name);
            if (obj) Object.assign(obj, detail);
        } catch (err) { console.error("Error parsing details", err); }
    });

    eventSource.addEventListener('image_status', (e) => {
        const statusData = JSON.parse(e.data);
        const obj = objects.value.find(o => o.name === statusData.name);
        if (obj) {
            obj.status = statusData.status;
            if (statusData.url) obj.image_url = statusData.url;
            if (statusData.image_fov) obj.image_fov = statusData.image_fov;
        }
    });

    eventSource.addEventListener('close', (e) => {
        streamStatus.value = 'Stream Complete';
        isDownloading.value = false;
        eventSource.close();
        eventSource = null;
    });

    eventSource.addEventListener('error', (e) => {
        streamStatus.value = 'Error/Disconnected';
        isDownloading.value = false;
        if (eventSource) eventSource.close();
        eventSource = null;
    });
};

const stopStream = () => {
    if (eventSource) {
        eventSource.close();
        eventSource = null;
        streamStatus.value = 'Stopped';
        isDownloading.value = false;
    }
};

const handlePurge = () => {
    objects.value = [];
    selectedObject.value = null;
    startStream('selected');
};

watch(selectedObject, async (newVal) => {
    if (newVal) {
        localStorage.setItem('lastSelectedId', newVal.name);
        if (!newVal.image_url || newVal.status === 'pending') {
            newVal.status = 'downloading';
            try {
                const res = await fetch('/api/download-object', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        object: newVal,
                        settings: settings.value
                    })
                });
                if (res.ok) {
                    const data = await res.json();
                    newVal.image_url = data.url;
                    newVal.status = data.status;
                } else {
                    newVal.status = 'error';
                }
            } catch (e) {
                console.error(e);
                newVal.status = 'error';
            }
        }
    }
});

const availableTypes = computed(() => {
    const types = new Set();
    objects.value.forEach(o => { if (o.type) types.add(o.type); });
    return Array.from(types).sort();
});

const streamParams = computed(() => {
    return {
        loc: settings.value.location,
        tel: settings.value.telescope,
        cam: settings.value.camera,
        cats: settings.value.catalogs,
        min_alt: settings.value.min_altitude,
        min_hrs: clientSettings.value.min_hours,
        sort: settings.value.sort_key,
        max_mag: clientSettings.value.max_magnitude,
        min_sz: clientSettings.value.min_size,
        types: clientSettings.value.selected_types,
        pad: settings.value.image_padding,
        img_srv: settings.value.image_server
    };
});

let restartTimer = null;
watch(streamParams, (newVal, oldVal) => {
    if (!oldVal) return;
    // Check for real changes
    const changes = [];
    for (const key in newVal) {
        if (JSON.stringify(newVal[key]) !== JSON.stringify(oldVal[key])) changes.push(key);
    }

    if (changes.length > 0) {
        if (restartTimer) clearTimeout(restartTimer);
        restartTimer = setTimeout(() => {
            startStream('selected');
        }, 1000);
    }
}, { deep: true });

const handleKeydown = (e) => {
    if (objects.value.length === 0) return;
    if (e.key === 'ArrowDown') {
        const idx = objects.value.findIndex(o => o.name === selectedObject.value?.name);
        if (idx < objects.value.length - 1) selectedObject.value = objects.value[idx + 1];
        else if (idx === -1) selectedObject.value = objects.value[0];
        e.preventDefault();
    } else if (e.key === 'ArrowUp') {
        const idx = objects.value.findIndex(o => o.name === selectedObject.value?.name);
        if (idx > 0) selectedObject.value = objects.value[idx - 1];
        e.preventDefault();
    }
};

onMounted(async () => {
    window.addEventListener('keydown', handleKeydown);
    await fetchSettings();
    startStream('selected');
});

watch(clientSettings, (newVal) => {
    saveSettings({});
}, { deep: true });

onUnmounted(() => {
    window.removeEventListener('keydown', handleKeydown);
});
</script>

<template>
    <div class="app-container">
        <TopBar :settings="settings" :streamStatus="streamStatus" :isDownloading="isDownloading"
            :downloadProgress="downloadProgress" @update-settings="saveSettings" @start-stream="startStream"
            @stop-stream="stopStream" @purge-cache="handlePurge" @download-filtered="startStream('filtered')"
            @download-all="startStream('all')" @stop-download="stopStream" />

        <div class="main-layout">
            <section class="framing-section">
                <div v-if="selectedObject" class="fill-height">
                    <Framing :object="selectedObject" :objects="objects" :settings="settings"
                        :clientSettings="clientSettings" :searchQuery="searchQuery"
                        @update-search="searchQuery = $event" @select-object="selectedObject = $event"
                        @update-settings="saveSettings"
                        @update-client-settings="Object.assign(clientSettings, $event)" />
                </div>
                <div v-else class="empty-state">
                    <h2>Select an object to view</h2>
                    <p>Configure settings in the top bar and start the search.</p>
                </div>
            </section>

            <section class="filter-section">
                <TypeFilter :availableTypes="availableTypes" :clientSettings="clientSettings"
                    @update-client-settings="Object.assign(clientSettings, $event)" />
            </section>

            <aside class="sidebar">
                <div class="graph-panel">
                    <AltitudeGraph :object="selectedObject" :location="settings.location" :nightTimes="nightTimes" />
                </div>
                <div class="list-panel">
                    <ObjectList :objects="objects" :selectedId="selectedObject?.name" :settings="settings"
                        :searchQuery="searchQuery" :clientSettings="clientSettings" :nightTimes="nightTimes"
                        @select="selectedObject = $event" @update-settings="saveSettings"
                        @update-client-settings="Object.assign(clientSettings, $event)"
                        @fetch-all="startStream('all')" />
                </div>
            </aside>
        </div>

        <footer class="bottom-bar">
            <div class="footer-content">
                <span>My Sky Observer v1.0</span>
                <span class="separator">|</span>
                <span>{{ objects.length }} Objects Loaded</span>
            </div>
        </footer>
    </div>
</template>

<style>
:root {
    --bg-color: #111827;
    --text-color: #f3f4f6;
    --border-color: #374151;
}

/* FIX: Set HTML background to match app background to prevent white borders */
html {
    background-color: var(--bg-color);
}

body {
    margin: 0;
    background-color: var(--bg-color);
    color: var(--text-color);
    font-family: sans-serif;
    height: 100vh;
    overflow: hidden;
}

.app-container {
    display: flex;
    flex-direction: column;
    height: 100vh;
}

.main-layout {
    display: flex;
    flex: 1;
    overflow: hidden;
}

.framing-section {
    flex: 1;
    border-right: 1px solid var(--border-color);
    position: relative;
    min-width: 0;
}

.filter-section {
    width: auto;
    border-right: 1px solid var(--border-color);
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
}

.sidebar {
    width: 450px;
    /* FIX: Increased width for readability */
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
}

.graph-panel {
    height: 200px;
    border-bottom: 1px solid var(--border-color);
    background: #000;
}

.list-panel {
    flex: 1;
    overflow: hidden;
}

.bottom-bar {
    height: 30px;
    background: #1f2937;
    border-top: 1px solid var(--border-color);
    display: flex;
    align-items: center;
    padding: 0 10px;
    font-size: 0.8rem;
    color: #9ca3af;
}

.footer-content {
    display: flex;
    gap: 10px;
}

.separator {
    color: #4b5563;
}
</style>