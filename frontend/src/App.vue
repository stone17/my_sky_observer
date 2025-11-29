<script setup>
import { ref, onMounted, watch, onUnmounted } from 'vue';
import TopBar from './components/TopBar.vue';
import ObjectList from './components/ObjectList.vue';
import Framing from './components/Framing.vue';
import AltitudeGraph from './components/AltitudeGraph.vue';

const settings = ref({});
const objects = ref([]);
const selectedObject = ref(null);
const streamStatus = ref('Idle');
const nightTimes = ref({});

// Stream handling
let eventSource = null;

const fetchSettings = async () => {
  try {
    const res = await fetch('/api/settings');
    if (res.ok) settings.value = await res.json();
  } catch (e) { console.error(e); }
};

const saveSettings = async (newSettings) => {
  try {
    await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(newSettings)
    });
    settings.value = newSettings;
  } catch (e) { console.error(e); }
};

const startStream = (forceDownload = false) => {
  if (eventSource) eventSource.close();
  
  objects.value = [];
  // Don't clear selectedObject immediately if we want to persist it, 
  // but we might want to validate it exists in the new stream.
  // For now, let's keep it null until we find it.
  const lastSelectedId = localStorage.getItem('lastSelectedId');
  selectedObject.value = null;
  
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
  let mode = settings.value.download_mode || 'selected';
  if (forceDownload) mode = 'all'; // Override for "Fetch All" action
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

  eventSource.addEventListener('object_data', (e) => {
    const obj = JSON.parse(e.data);
    objects.value.push(obj);
    
    // Auto-select if matches last selection
    if (lastSelectedId && obj.name === lastSelectedId && !selectedObject.value) {
        selectedObject.value = obj;
    }
    // If no last selection, maybe select the first one? 
    // User asked: "I want to see the first target directly (based on last selection)"
    // If no last selection, selecting the first one is a good default.
    else if (!lastSelectedId && objects.value.length === 1) {
        selectedObject.value = obj;
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
  });

  eventSource.addEventListener('error', (e) => {
     streamStatus.value = 'Error/Disconnected';
     if(eventSource) eventSource.close();
     eventSource = null;
  });
};

const stopStream = () => {
    if (eventSource) {
        eventSource.close();
        eventSource = null;
        streamStatus.value = 'Stopped';
    }
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
  await fetchSettings();
  startStream(); // Auto-start
});

onUnmounted(() => {
    window.removeEventListener('keydown', handleKeydown);
});
</script>

<template>
  <div class="app-container">
    <TopBar
        :settings="settings"
        :streamStatus="streamStatus"
        @update-settings="saveSettings"
        @start-stream="startStream"
        @stop-stream="stopStream"
        @purge-cache="handlePurge"
    />

    <div class="main-layout">
      <!-- Left: Main Framing -->
      <section class="framing-section">
        <div v-if="selectedObject" class="fill-height">
             <Framing :object="selectedObject" :settings="settings" />
        </div>
        <div v-else class="empty-state">
             <h2>Select an object to view</h2>
             <p>Configure settings in the top bar and start the search.</p>
        </div>
      </section>

      <!-- Right: Graph + List -->
      <aside class="sidebar">
        <div class="graph-panel">
             <AltitudeGraph
                :object="selectedObject"
                :location="settings.location"
                :nightTimes="nightTimes"
             />
        </div>
        <div class="list-panel">
             <ObjectList
                :objects="objects"
                :selectedId="selectedObject?.name"
                :settings="settings"
                @select="selectedObject = $event"
                @update-settings="saveSettings"
                @fetch-all="startStream(true)"
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
    flex: 2; /* Takes more space */
    border-right: 1px solid var(--border-color);
    position: relative;
}

.sidebar {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-width: 300px;
    max-width: 450px;
}

.graph-panel {
    height: 250px; /* Increased height as requested */
    border-bottom: 1px solid var(--border-color);
}

.list-panel {
    flex: 1;
    overflow-y: auto;
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

/* Scrollbar styling */
::-webkit-scrollbar { width: 8px; }
::-webkit-scrollbar-track { background: #1f2937; }
::-webkit-scrollbar-thumb { background: #4b5563; border-radius: 4px; }
::-webkit-scrollbar-thumb:hover { background: #6b7280; }
</style>