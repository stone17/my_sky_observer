<script setup>
import { ref, onMounted, watch } from 'vue';
import Settings from './components/Settings.vue';
import ObjectList from './components/ObjectList.vue';
import Framing from './components/Framing.vue';

const settings = ref({});
const objects = ref([]);
const selectedObject = ref(null);
const streamStatus = ref('Idle');
const totalObjects = ref(0);
const isSettingsCollapsed = ref(false);

const fetchSettings = async () => {
  try {
    const res = await fetch('/api/settings');
    if (res.ok) settings.value = await res.json();
  } catch (e) {
    console.error("Failed to load settings", e);
  }
};

const saveSettings = async (newSettings) => {
  try {
    await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(newSettings)
    });
    settings.value = newSettings;
  } catch (e) {
    console.error("Failed to save settings", e);
  }
};

let eventSource = null;

const startStream = () => {
  if (eventSource) eventSource.close();
  
  objects.value = []; // Clear list
  selectedObject.value = null;
  streamStatus.value = 'Connecting...';
  totalObjects.value = 0;

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

  eventSource.onmessage = (event) => {
    // Generic message handler if needed
  };

  eventSource.addEventListener('total', (e) => {
    totalObjects.value = parseInt(e.data);
    streamStatus.value = `Found ${totalObjects.value} objects.`;
  });

  eventSource.addEventListener('object_data', (e) => {
    const obj = JSON.parse(e.data);
    objects.value.push(obj);
    // If it's the first object and none selected, select it? Maybe not, let user choose.
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
    console.error("Stream error", e);
    if (e.data) {
         const err = JSON.parse(e.data);
         streamStatus.value = `Error: ${err.error}`;
    } else {
        streamStatus.value = 'Connection Error';
    }
    eventSource.close();
    eventSource = null;
  });
};

const stopStream = () => {
    if (eventSource) {
        eventSource.close();
        eventSource = null;
        streamStatus.value = 'Stopped';
    }
}

onMounted(() => {
  fetchSettings();
});
</script>

<template>
  <div class="container-fluid">
    <header>
      <nav>
        <ul>
          <li><strong>My Sky Observer</strong></li>
        </ul>
        <ul>
          <li><small>{{ streamStatus }}</small></li>
          <li><button class="outline" @click="stopStream" v-if="streamStatus.includes('Connecting') || streamStatus.includes('Found')">Stop</button></li>
        </ul>
      </nav>
    </header>

    <div class="layout-grid" :class="{ 'collapsed-settings': isSettingsCollapsed }">
      <aside>
        <div v-if="!isSettingsCollapsed">
            <Settings :settings="settings" @update="saveSettings" @start="startStream" @collapse="isSettingsCollapsed = true" />
            <hr />
        </div>
        <div v-else style="margin-bottom: 10px;">
             <button class="outline secondary" @click="isSettingsCollapsed = false" style="width: 100%">Show Settings</button>
        </div>
        <ObjectList :objects="objects" :selectedId="selectedObject?.name" @select="selectedObject = $event" />
      </aside>

      <main>
        <div v-if="selectedObject">
           <Framing :object="selectedObject" />
        </div>
        <div v-else class="empty-state">
          <article>
            <header>Select an object</header>
            <p>Configure your equipment and location on the left, start the search, and select an object to view framing details.</p>
          </article>
        </div>
      </main>
    </div>
  </div>
</template>

<style scoped>
.empty-state {
  display: flex;
  justify-content: center;
  align-items: center;
  height: 100%;
  text-align: center;
}
.collapsed-settings {
    /* Adjust grid if sidebar needs to be smaller, though standard sidebar is fine */
}
</style>