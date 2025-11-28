<script setup>
import { ref, onMounted, onUnmounted, watch, computed } from 'vue';

const props = defineProps({
  settings: Object,
  streamStatus: String
});

const emit = defineEmits(['update-settings', 'start-stream', 'stop-stream', 'purge-cache']);

const localSettings = ref({});
const profiles = ref({});
const presets = ref({ cameras: {} });
const cacheStatus = ref({ size_mb: 0, count: 0 });
const selectedProfileName = ref(null);

const activeDropdown = ref(null);

// Location Search
const cityQuery = ref("");
const cityResults = ref([]);
const isSearchingCity = ref(false);

// Watch for prop updates with deep copy and safety checks
watch(() => props.settings, (newVal) => {
  if (newVal) {
    try {
        // Keep track if we are initializing
        const isInit = Object.keys(localSettings.value).length === 0;

        localSettings.value = JSON.parse(JSON.stringify(newVal));

        // Ensure sub-objects exist
        if (!localSettings.value.telescope) localSettings.value.telescope = {};
        if (!localSettings.value.camera) localSettings.value.camera = {};
        if (!localSettings.value.location) localSettings.value.location = {};
        if (localSettings.value.min_altitude === undefined) localSettings.value.min_altitude = 30.0;
        if (localSettings.value.min_hours === undefined) localSettings.value.min_hours = 0.0;

    } catch(e) {
        console.error("Error parsing settings prop:", e);
    }
  }
}, { immediate: true, deep: true });

const toggleDropdown = (name) => {
  activeDropdown.value = activeDropdown.value === name ? null : name;
};

const closeDropdown = () => {
  activeDropdown.value = null;
};

// Data Fetching
const fetchProfiles = async () => {
  try {
    const res = await fetch('/api/profiles');
    if (res.ok) {
        profiles.value = await res.json();

        // Auto-load last used profile logic
        const lastProfile = localStorage.getItem('last_profile');
        if (lastProfile && profiles.value[lastProfile]) {
            // Just set the name, don't overwrite settings from the profile template
            // We want to respect the settings loaded from the server (settings.json)
            selectedProfileName.value = lastProfile;
        }
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

onMounted(() => {
  fetchProfiles();
  fetchPresets();
  fetchCacheStatus();
  // Poll cache status every 10s
  const interval = setInterval(fetchCacheStatus, 10000);
  onUnmounted(() => clearInterval(interval));
});

// Actions
const loadProfile = (name) => {
  if (profiles.value[name]) {
    localSettings.value = JSON.parse(JSON.stringify(profiles.value[name]));
    // Ensure defaults
    if (localSettings.value.min_altitude === undefined) localSettings.value.min_altitude = 30.0;
    if (localSettings.value.min_hours === undefined) localSettings.value.min_hours = 0.0;

    selectedProfileName.value = name;
    // Save to localStorage for next time
    localStorage.setItem('last_profile', name);
    emit('update-settings', localSettings.value);
    closeDropdown();
  }
};

const applyPreset = (cameraName) => {
    const cam = presets.value.cameras[cameraName];
    if (cam && localSettings.value.camera) {
        localSettings.value.camera.sensor_width = cam.width;
        localSettings.value.camera.sensor_height = cam.height;
        emit('update-settings', localSettings.value);
    }
};

// Computed property for the camera preset select
const selectedCameraPreset = computed({
    get() {
        if (!localSettings.value.camera || !presets.value.cameras) return "";
        const w = localSettings.value.camera.sensor_width;
        const h = localSettings.value.camera.sensor_height;
        // Find key with matching width/height (allow small float diff)
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
    if(!confirm("Are you sure you want to purge the cache? This will delete all downloaded images.")) return;
    await fetch('/api/cache/purge', { method: 'POST' });
    await fetchCacheStatus();
    emit('purge-cache'); // Trigger parent reload if needed
};

const redownloadCache = () => {
    if(confirm("Purge and Redownload?")) {
        purgeCache();
        emit('start-stream'); // Restarting stream will redownload images since cache is gone
    }
};

const searchCity = async () => {
    if (!cityQuery.value || cityQuery.value.length < 2) return;
    isSearchingCity.value = true;
    try {
        const res = await fetch(`/api/geocode?city=${encodeURIComponent(cityQuery.value)}`);
        if (res.ok) {
            cityResults.value = await res.json();
        }
    } catch(e) { console.error(e); }
    finally { isSearchingCity.value = false; }
};

const selectCity = (city) => {
    if (!localSettings.value.location) localSettings.value.location = {};
    localSettings.value.location.latitude = city.latitude;
    localSettings.value.location.longitude = city.longitude;
    localSettings.value.location.city_name = city.name;
    emit('update-settings', localSettings.value);
    cityResults.value = [];
    cityQuery.value = "";
    closeDropdown();
};

const fovDisplay = computed(() => {
    if (!localSettings.value.telescope || !localSettings.value.camera) return "-";
    const fl = localSettings.value.telescope.focal_length;
    const sw = localSettings.value.camera.sensor_width;
    const sh = localSettings.value.camera.sensor_height;
    if (!fl) return "-";
    const w = ((sw / fl) * 57.2958).toFixed(2);
    const h = ((sh / fl) * 57.2958).toFixed(2);
    return `${w}° x ${h}°`;
});

const locationDisplay = computed(() => {
    const loc = localSettings.value.location;
    if (!loc) return "Location";
    if (loc.city_name) return loc.city_name;
    if (loc.latitude !== undefined && loc.longitude !== undefined) {
        return `${loc.latitude.toFixed(2)}, ${loc.longitude.toFixed(2)}`;
    }
    return "Location";
});

</script>

<template>
  <div class="top-bar">
    <!-- Profile -->
    <div class="tb-item relative">
      <button class="tb-btn" @click="toggleDropdown('profile')">
        Profile: {{ selectedProfileName || 'Custom' }} ▼
      </button>
      <div class="dropdown-menu" v-if="activeDropdown === 'profile'">
        <label><strong>Load Profile</strong></label>
        <ul class="link-list">
            <li v-for="(p, name) in profiles" :key="name" @click="loadProfile(name)">{{ name }}</li>
        </ul>
        <hr/>
        <div class="field-group" v-if="localSettings.telescope">
             <label>Focal Length (mm)</label>
             <input type="number" v-model.number="localSettings.telescope.focal_length" @change="$emit('update-settings', localSettings)" />
        </div>
        <label><strong>Camera Settings</strong></label>
         <select v-model="selectedCameraPreset" class="mini-select">
            <option value="" disabled>Custom / Select Preset...</option>
            <option v-for="(specs, name) in presets.cameras" :key="name" :value="name">{{ name }}</option>
        </select>
        <div class="field-group" v-if="localSettings.camera">
             <label>Sensor Width (mm)</label>
             <input type="number" step="0.1" v-model.number="localSettings.camera.sensor_width" @change="$emit('update-settings', localSettings)" />
        </div>
        <div class="field-group" v-if="localSettings.camera">
             <label>Sensor Height (mm)</label>
             <input type="number" step="0.1" v-model.number="localSettings.camera.sensor_height" @change="$emit('update-settings', localSettings)" />
        </div>
      </div>
    </div>

    <!-- Location -->
    <div class="tb-item relative">
      <button class="tb-btn" @click="toggleDropdown('location')">
        {{ locationDisplay }} ▼
      </button>
      <div class="dropdown-menu" v-if="activeDropdown === 'location'">
        <div class="field-group" v-if="localSettings.location">
             <label>Latitude</label>
             <input type="number" step="0.0001" v-model.number="localSettings.location.latitude" @change="$emit('update-settings', localSettings)" />
        </div>
        <div class="field-group" v-if="localSettings.location">
             <label>Longitude</label>
             <input type="number" step="0.0001" v-model.number="localSettings.location.longitude" @change="$emit('update-settings', localSettings)" />
        </div>
        <hr/>
        <label><strong>Search City</strong></label>
        <div style="display: flex; gap: 5px;">
            <input type="text" v-model="cityQuery" @keyup.enter="searchCity" placeholder="City name..." style="flex:1; background: black; border: 1px solid #444; color: white; padding: 5px;" />
            <button class="small" @click="searchCity">{{ isSearchingCity ? '...' : 'Go' }}</button>
        </div>
        <ul v-if="cityResults.length > 0" class="link-list" style="max-height: 150px; overflow-y: auto;">
            <li v-for="(city, idx) in cityResults" :key="idx" @click="selectCity(city)">
                {{ city.name }}, {{ city.country }}
            </li>
        </ul>
      </div>
    </div>

    <!-- FOV -->
    <div class="tb-item">
      <span>FOV: {{ fovDisplay }}</span>
    </div>


    <!-- Cache -->
    <div class="tb-item relative">
      <button class="tb-btn" @click="toggleDropdown('cache')">
        Cache: {{ cacheStatus.size_mb }} MB ▼
      </button>
      <div class="dropdown-menu" v-if="activeDropdown === 'cache'">
         <div class="info-row">Images: {{ cacheStatus.count }}</div>
         <button class="danger small full-width" @click="purgeCache">Purge Cache</button>
         <button class="secondary small full-width" @click="redownloadCache" style="margin-top: 5px">Redownload</button>
      </div>
    </div>

    <!-- Stream Controls -->
    <div class="tb-item push-right">
         <span class="status-text">{{ streamStatus }}</span>
         <button v-if="streamStatus === 'Idle' || streamStatus === 'Stopped' || streamStatus.includes('Complete')" @click="$emit('start-stream')">Start</button>
         <button v-else class="outline" @click="$emit('stop-stream')">Stop</button>
    </div>
  </div>
</template>

<style scoped>
.top-bar {
  display: flex;
  align-items: center;
  background: #1f2937;
  padding: 0 1rem;
  height: 50px;
  border-bottom: 1px solid #374151;
  font-size: 0.9rem;
  gap: 1rem;
}

.tb-item {
    display: flex;
    align-items: center;
}

.tb-btn {
    background: transparent;
    border: none;
    color: #e5e7eb;
    cursor: pointer;
    padding: 5px 10px;
    font-size: inherit;
}
.tb-btn:hover {
    background: rgba(255,255,255,0.1);
    border-radius: 4px;
}

.push-right {
    margin-left: auto;
    gap: 10px;
}

.relative {
    position: relative;
}

.dropdown-menu {
    position: absolute;
    top: 100%;
    left: 0;
    background: #111827;
    border: 1px solid #374151;
    padding: 10px;
    z-index: 100;
    min-width: 220px;
    border-radius: 0 0 4px 4px;
    box-shadow: 0 4px 6px rgba(0,0,0,0.3);
}

.link-list {
    list-style: none;
    padding: 0;
    margin: 5px 0;
}
.link-list li {
    padding: 5px;
    cursor: pointer;
}
.link-list li:hover {
    background: #374151;
}

.mini-select {
    width: 100%;
    margin-bottom: 10px;
    padding: 5px;
    background: #000;
    color: white;
    border: 1px solid #444;
}

.field-group {
    margin-bottom: 5px;
}
.field-group label {
    display: block;
    font-size: 0.8em;
    color: #9ca3af;
}
.field-group input {
    width: 100%;
    padding: 5px;
    background: #000;
    border: 1px solid #444;
    color: white;
}

.checkbox-row {
    display: flex;
    align-items: center;
    gap: 5px;
    margin-bottom: 5px;
}

.full-width { width: 100%; }
.small { padding: 2px 8px; font-size: 0.8rem; }
.danger { background-color: #ef4444; border-color: #ef4444; color: white; }
.secondary { background-color: #4b5563; border-color: #4b5563; color: white; }

.status-text {
    color: #9ca3af;
    font-size: 0.8rem;
    margin-right: 10px;
}
</style>