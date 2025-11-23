<script setup>
import { ref, onMounted, watch } from 'vue';
import TopBar from './components/TopBar.vue';
import ObjectList from './components/ObjectList.vue';
import Framing from './components/Framing.vue';
import AltitudeGraph from './components/AltitudeGraph.vue';

const settings = ref({});
const objects = ref([]);
const selectedObject = ref(null);
const streamStatus = ref('Idle');

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

const startStream = () => {
  if (eventSource) eventSource.close();
  
  objects.value = [];
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
  params.append('image_padding', settings.value.image_padding || 1.05);

  eventSource = new EventSource(`/api/stream-objects?${params.toString()}`);

  eventSource.addEventListener('total', (e) => {
    streamStatus.value = `Found ${e.data} objects.`;
  });

  eventSource.addEventListener('object_data', (e) => {
    const obj = JSON.parse(e.data);
    objects.value.push(obj);
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
    // Optionally restart stream?
};

onMounted(() => {
  fetchSettings();
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
             <AltitudeGraph :object="selectedObject" :location="settings.location" />
        </div>
        <div class="list-panel">
             <ObjectList :objects="objects" :selectedId="selectedObject?.name" @select="selectedObject = $event" />
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
    height: 150px; /* Fixed height for graph */
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