<script setup>
import { ref, watch, onMounted } from 'vue';

const props = defineProps(['settings']);
const emit = defineEmits(['update', 'start']);

const localSettings = ref({});
const presets = ref({ cameras: {} });

// Deep copy settings to local state to avoid mutating props directly
watch(() => props.settings, (newVal) => {
  if (newVal) localSettings.value = JSON.parse(JSON.stringify(newVal));
}, { immediate: true, deep: true });

onMounted(async () => {
    try {
        const res = await fetch('/api/presets');
        presets.value = await res.json();
    } catch (e) { console.error(e); }
});

const applyPreset = (cameraName) => {
    const cam = presets.value.cameras[cameraName];
    if (cam && localSettings.value.camera) {
        localSettings.value.camera.sensor_width = cam.width;
        localSettings.value.camera.sensor_height = cam.height;
    }
};

const geocodeCity = async () => {
    // Implementation for geocoding if needed, for now manual input
    // Could add a city search input later
};

const saveAndStart = () => {
    emit('update', localSettings.value);
    emit('start');
};
</script>

<template>
  <article>
    <header>Configuration</header>
    <form @submit.prevent="saveAndStart">
        <details open>
            <summary>Telescope & Camera</summary>
            <div class="grid">
                <label>
                    Focal Length (mm)
                    <input type="number" v-model.number="localSettings.telescope.focal_length" required />
                </label>
            </div>
            <div class="grid">
                <label>
                    Preset
                    <select @change="applyPreset($event.target.value)">
                        <option value="" disabled selected>Select Camera</option>
                        <option v-for="(specs, name) in presets.cameras" :key="name" :value="name">{{ name }}</option>
                    </select>
                </label>
            </div>
             <div class="grid">
                <label>
                    Sensor W (mm)
                    <input type="number" step="0.1" v-model.number="localSettings.camera.sensor_width" required />
                </label>
                <label>
                    Sensor H (mm)
                    <input type="number" step="0.1" v-model.number="localSettings.camera.sensor_height" required />
                </label>
            </div>
        </details>

        <details>
            <summary>Location</summary>
            <div class="grid">
                <label>
                    Lat
                    <input type="number" step="0.0001" v-model.number="localSettings.location.latitude" required />
                </label>
                <label>
                    Lon
                    <input type="number" step="0.0001" v-model.number="localSettings.location.longitude" required />
                </label>
            </div>
        </details>

        <details>
            <summary>Filters</summary>
             <label>
                Catalogs
                <select v-model="localSettings.catalogs" multiple style="height: 100px">
                    <option value="messier">Messier</option>
                    <option value="ngc">NGC (Example)</option>
                </select>
            </label>
            <div class="grid">
                <label>
                    Min Alt (deg)
                    <input type="number" v-model.number="localSettings.min_altitude" />
                </label>
                <label>
                    Sort By
                    <select v-model="localSettings.sort_key">
                        <option value="time">Best Time (Altitude)</option>
                        <option value="brightness">Brightness</option>
                        <option value="size">Size</option>
                    </select>
                </label>
            </div>
        </details>

        <button type="submit">Start Search</button>
    </form>
  </article>
</template>