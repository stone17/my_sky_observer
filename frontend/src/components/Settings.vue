<script setup>
import { ref, watch, onMounted } from 'vue';

const props = defineProps(['settings']);
const emit = defineEmits(['update', 'start']);

const localSettings = ref({});
const presets = ref({ cameras: {} });
const profiles = ref({});
const newProfileName = ref("");

// Deep copy settings to local state to avoid mutating props directly
watch(() => props.settings, (newVal) => {
  if (newVal) {
      localSettings.value = JSON.parse(JSON.stringify(newVal));
      // Ensure structure exists to prevent crashes
      if (!localSettings.value.telescope) localSettings.value.telescope = {};
      if (!localSettings.value.camera) localSettings.value.camera = {};
      if (!localSettings.value.location) localSettings.value.location = {};
  }
}, { immediate: true, deep: true });

const fetchProfiles = async () => {
    try {
        const res = await fetch('/api/profiles');
        if (res.ok) profiles.value = await res.json();
    } catch (e) { console.error(e); }
};

onMounted(async () => {
    try {
        const res = await fetch('/api/presets');
        presets.value = await res.json();
        await fetchProfiles();
    } catch (e) { console.error(e); }
});

const applyPreset = (cameraName) => {
    const cam = presets.value.cameras[cameraName];
    if (cam && localSettings.value.camera) {
        localSettings.value.camera.sensor_width = cam.width;
        localSettings.value.camera.sensor_height = cam.height;
    }
};

const saveProfile = async () => {
    if (!newProfileName.value) return;
    try {
        const res = await fetch(`/api/profiles/${newProfileName.value}`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(localSettings.value)
        });
        if (res.ok) {
            await fetchProfiles();
            newProfileName.value = "";
            alert("Profile saved");
        }
    } catch (e) { alert("Error saving profile"); }
};

const loadProfile = (name) => {
    if (profiles.value[name]) {
        localSettings.value = JSON.parse(JSON.stringify(profiles.value[name]));
    }
};

const deleteProfile = async (name) => {
    if (!confirm(`Delete profile ${name}?`)) return;
    try {
        const res = await fetch(`/api/profiles/${name}`, { method: 'DELETE' });
        if (res.ok) await fetchProfiles();
    } catch (e) { alert("Error deleting profile"); }
};

const saveAndStart = () => {
    emit('update', localSettings.value);
    emit('start');
};
</script>

<template>
  <article>
    <header>
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <span>Configuration</span>
            <button class="outline secondary" style="padding: 5px 10px; font-size: 0.8rem;" @click="$emit('collapse')">Hide</button>
        </div>
    </header>
    <form @submit.prevent="saveAndStart" v-if="localSettings.telescope && localSettings.camera && localSettings.location">
        <details>
            <summary>Profiles</summary>
             <div class="grid">
                <label>
                    Load Profile
                    <select @change="loadProfile($event.target.value)">
                        <option value="" disabled selected>Select...</option>
                        <option v-for="(data, name) in profiles" :key="name" :value="name">{{ name }}</option>
                    </select>
                </label>
            </div>
             <div class="grid">
                <label>
                    New Profile Name
                    <input type="text" v-model="newProfileName" placeholder="My Setup" />
                </label>
                <button type="button" class="secondary" @click="saveProfile" style="margin-top: 25px">Save</button>
            </div>
        </details>

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
            <div class="grid">
                 <label>
                    Image Padding (x FOV)
                    <input type="number" step="0.05" v-model.number="localSettings.image_padding" />
                    <small>1.05 = 5% larger</small>
                </label>
            </div>
        </details>

        <button type="submit">Start Search</button>
    </form>
    <div v-else>Loading settings...</div>
  </article>
</template>