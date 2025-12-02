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
    selected_types: []
});
const objects = ref([]);
const selectedObject = ref(null);
const streamStatus = ref('Idle');
const nightTimes = ref({});
const activeDownloadMode = ref('none'); // none, filtered, all

// Stream handling
let eventSource = null;

const fetchSettings = async () => {
    try {
        const res = await fetch('/api/settings');
        if (res.ok) {
            const data = await res.json();
            settings.value = data;

            // Load client settings from backend if available
            if (data.client_settings) {
                // Merge to preserve defaults/structure
                Object.assign(clientSettings.value, data.client_settings);
            }
        }
    } catch (e) { console.error(e); }
};

const saveSettings = async (newSettings) => {
    // console.log("DEBUG: saveSettings called with:", newSettings);
    try {
        // Ensure we send both main settings and client settings
        const payload = {
            ...settings.value, // Current global settings
            ...newSettings,    // Overwrites from TopBar if any
            client_settings: clientSettings.value // Ensure client settings are included
        };

        // Remove obsolete download_mode if present
        delete payload.download_mode;

        await fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        // Update local ref
        settings.value = payload;

    } catch (e) { console.error(e); }
};

const startStream = (modeOverride = null) => {
    if (eventSource) eventSource.close();

    streamStatus.value = 'Connecting...';

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
    params.append('min_hours', settings.value.min_hours || 0.0);
    params.append('image_padding', settings.value.image_padding || 1.05);

    // Download Mode Logic
    // Default to 'selected' unless overridden by buttons
    let mode = modeOverride || 'selected';

    // Update active mode state
    if (mode === 'all' || mode === 'filtered') {
        activeDownloadMode.value = mode;
    } else {
        activeDownloadMode.value = 'none';
    }

    params.append('download_mode', mode);

    eventSource = new EventSource(`/api/stream-objects?${params.toString()}`);

    eventSource.addEventListener('total', (e) => {
        streamStatus.value = `Found ${e.data} objects.`;
    });

    eventSource.addEventListener('night_times', (e) => {
        try {
            nightTimes.value = JSON.parse(e.data);
        } catch (e) { console.error("Error parsing night times", e); }
    });

    // 1. Metadata Event: Receive full list immediately
    eventSource.addEventListener('catalog_metadata', (e) => {
        try {
            const newObjects = JSON.parse(e.data);
            console.log(`Received metadata for ${newObjects.length} objects`);

            // Preserve selection
            const currentSelectedId = selectedObject.value?.name;

            // Update list immediately
            objects.value = newObjects;

            // Restore selection or Auto-select top item
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

    // 2. Details Event: Update specific objects with graphs/images
    eventSource.addEventListener('object_details', (e) => {
        try {
            const detail = JSON.parse(e.data);
            const obj = objects.value.find(o => o.name === detail.name);
            if (obj) {
                // Merge details into existing object
                Object.assign(obj, detail);
            }
        } catch (err) {
            console.error("Error parsing details", err);
        }
    });

    eventSource.addEventListener('image_status', (e) => {
        const statusData = JSON.parse(e.data);
        const obj = objects.value.find(o => o.name === statusData.name);
        if (obj) {
            obj.status = statusData.status;
            if (statusData.url) obj.image_url = statusData.url;
        }
    });

    eventSource.addEventListener('close', (e) => {
        streamStatus.value = 'Stream Complete';
        eventSource.close();
        eventSource = null;
        // Reset active mode when finished naturally
        if (activeDownloadMode.value !== 'none') {
             // Keep it? Or reset? Usually completion means download done.
             // But if we restart stream it resets.
             // Let's reset it to indicate the process finished.
             activeDownloadMode.value = 'none';
        }
    });

    eventSource.addEventListener('error', (e) => {
        streamStatus.value = 'Error/Disconnected';
        if (eventSource) eventSource.close();
        eventSource = null;
        activeDownloadMode.value = 'none';
    });
};

const stopStream = () => {
    if (eventSource) {
        eventSource.close();
        eventSource = null;
        streamStatus.value = 'Stopped';
    }
    activeDownloadMode.value = 'none';
};

const handleStartDownload = (mode) => {
    startStream(mode);
};

const handleStopDownload = () => {
    // Restarting with 'selected' effectively stops the bulk download but keeps the list live
    startStream('selected');
};

// Handle cache purge reload
const handlePurge = () => {
    objects.value = [];
    selectedObject.value = null;
    startStream(); // Restart automatically
};

// Watch selection to persist and fetch image if needed
watch(selectedObject, async (newVal) => {
    if (newVal) {
        localStorage.setItem('lastSelectedId', newVal.name);

        // If in "selected" mode (or any mode really) and image is missing/pending, fetch it
        if (!newVal.image_url || newVal.status === 'pending') {
            console.log(`Fetching image for ${newVal.name}...`);
            // Optimistic update
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
                    console.error("Failed to fetch image");
                    newVal.status = 'error';
                }
            } catch (e) {
                console.error(e);
                newVal.status = 'error';
            }
        }
    }
});

// Compute available types for filter
const availableTypes = computed(() => {
    const types = new Set();
    objects.value.forEach(o => {
        if (o.type) types.add(o.type);
    });
    return Array.from(types).sort();
});

// Auto-restart stream on settings change (Debounced)
// Only watch parameters that require a backend reload
const streamParams = computed(() => {
    return {
        loc: settings.value.location,
        tel: settings.value.telescope,
        cam: settings.value.camera,
        cats: settings.value.catalogs,
        // mode: settings.value.download_mode, // REMOVED: download_mode is now action-based
        min_alt: settings.value.min_altitude,
        min_hrs: settings.value.min_hours,
        pad: settings.value.image_padding
    };
});

let restartTimer = null;
watch(streamParams, (newVal, oldVal) => {
    // Skip if initial load (empty oldVal)
    if (!oldVal) return;

    // DEBUG: Identify what changed
    const changes = [];
    for (const key in newVal) {
        if (JSON.stringify(newVal[key]) !== JSON.stringify(oldVal[key])) {
            changes.push(key);
        }
    }

    if (changes.length > 0) {
        console.log("DEBUG: Stream params changed:", changes);
        console.log("Old:", oldVal);
        console.log("New:", newVal);

        if (restartTimer) clearTimeout(restartTimer);

        restartTimer = setTimeout(() => {
            console.log("Stream params changed, restarting stream...");
            startStream();
        }, 1000);
    } else {
        // console.log("DEBUG: Stream params watcher fired but no changes detected.");
    }
}, { deep: true });

const handleKeydown = (e) => {
    // Only handle if we have objects
    if (objects.value.length === 0) return;

    if (e.key === 'ArrowDown') {
        const idx = objects.value.findIndex(o => o.name === selectedObject.value?.name);
        if (idx < objects.value.length - 1) {
            selectedObject.value = objects.value[idx + 1];
        } else if (idx === -1 && objects.value.length > 0) {
            selectedObject.value = objects.value[0];
        }
        e.preventDefault();
    } else if (e.key === 'ArrowUp') {
        const idx = objects.value.findIndex(o => o.name === selectedObject.value?.name);
        if (idx > 0) {
            selectedObject.value = objects.value[idx - 1];
        }
        e.preventDefault();
    }
};

onMounted(async () => {
    window.addEventListener('keydown', handleKeydown);

    // No longer loading from localStorage

    await fetchSettings();
    startStream(); // Auto-start
});

// Watch for changes to persist client settings
watch(clientSettings, (newVal) => {
    // Debounce or just save? Save is cheap enough for now.
    saveSettings({}); // Pass empty object to trigger save with current state
}, { deep: true });

onUnmounted(() => {
    window.removeEventListener('keydown', handleKeydown);
});
</script>

<template>
    <div class="app-container">
        <TopBar :settings="settings" :streamStatus="streamStatus" :activeDownloadMode="activeDownloadMode" @update-settings="saveSettings"
            @start-stream="startStream" @stop-stream="stopStream" @purge-cache="handlePurge"
            @start-download="handleStartDownload" @stop-download="handleStopDownload" />

        <div class="main-layout">
            <!-- Left: Main Framing -->
            <section class="framing-section">
                <div v-if="selectedObject" class="fill-height">
                    <Framing :object="selectedObject" :settings="settings" :clientSettings="clientSettings"
                        @update-settings="saveSettings"
                        @update-client-settings="Object.assign(clientSettings, $event)" />
                </div>
                <div v-else class="empty-state">
                    <h2>Select an object to view</h2>
                    <p>Configure settings in the top bar and start the search.</p>
                </div>
            </section>

            <!-- Middle: Vertical Type Filter -->
            <section class="filter-section">
                <TypeFilter :availableTypes="availableTypes" :clientSettings="clientSettings"
                    @update-client-settings="Object.assign(clientSettings, $event)" />
            </section>

            <!-- Right: Graph + List -->
            <aside class="sidebar">
                <div class="graph-panel">
                    <AltitudeGraph :object="selectedObject" :location="settings.location" :nightTimes="nightTimes" />
                </div>
                <div class="list-panel">
                    <ObjectList :objects="objects" :selectedId="selectedObject?.name" :settings="settings"
                        :clientSettings="clientSettings" :nightTimes="nightTimes" @select="selectedObject = $event"
                        @update-settings="saveSettings" @update-client-settings="Object.assign(clientSettings, $event)"
                        @fetch-all="handleStartDownload('all')" />
                </div>
            </aside>
        </div>

        <!-- Bottom Bar (Footer) -->
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
    /* Takes remaining space */
    border-right: 1px solid var(--border-color);
    position: relative;
    min-width: 0;
    /* Allow shrinking */
}

.filter-section {
    width: auto;
    /* Width determined by content (TypeFilter width) */
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
    width: 400px;
    /* Fixed width for sidebar */
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