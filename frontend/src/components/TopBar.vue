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

const activeDropdown = ref(null);

// Sorting options
const sortOptions = ref({
  time: { label: 'Best Time (Altitude)', enabled: true },
  hours_above: { label: 'Hours Visible', enabled: false },
  brightness: { label: 'Brightness', enabled: false },
  size: { label: 'Size', enabled: false }
});

// Watch for prop updates with deep copy and safety checks
watch(() => props.settings, (newVal) => {
  if (newVal) {
    try {
        localSettings.value = JSON.parse(JSON.stringify(newVal));

        // Ensure sub-objects exist
        if (!localSettings.value.telescope) localSettings.value.telescope = {};
        if (!localSettings.value.camera) localSettings.value.camera = {};
        if (!localSettings.value.location) localSettings.value.location = {};

        // Initialize sort options based on current setting
        const currentSort = localSettings.value.sort_key || 'time';
        const keys = currentSort.split(',');
        for (const k in sortOptions.value) {
            sortOptions.value[k].enabled = keys.includes(k);
        }
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
            console.log("Auto-loading profile:", lastProfile);
            loadProfile(lastProfile);
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

const updateSort = () => {
    const keys = Object.keys(sortOptions.value).filter(k => sortOptions.value[k].enabled);
    if (keys.length === 0) {
        // Default fallback
        sortOptions.value.time.enabled = true;
        keys.push('time');
    }
    localSettings.value.sort_key = keys.join(',');
    emit('update-settings', localSettings.value);
};

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

</script>

<template>
  <div class="top-bar">
    <!-- Profile -->
    <div class="tb-item relative">
      <button class="tb-btn" @click="toggleDropdown('profile')">
        Profile: {{ localSettings.telescope?.focal_length ? 'Custom' : 'Select' }} ▼
      </button>
      <div class="dropdown-menu" v-if="activeDropdown === 'profile'">
        <label><strong>Load Profile</strong></label>
        <ul class="link-list">
            <li v-for="(p, name) in profiles" :key="name" @click="loadProfile(name)">{{ name }}</li>
        </ul>
        <hr/>
        <label><strong>Camera Preset</strong></label>
         <select @change="applyPreset($event.target.value)" class="mini-select">
            <option value="" disabled selected>Select...</option>
            <option v-for="(specs, name) in presets.cameras" :key="name" :value="name">{{ name }}</option>
        </select>
        <div class="field-group" v-if="localSettings.telescope">
             <label>Focal Length (mm)</label>
             <input type="number" v-model.number="localSettings.telescope.focal_length" @change="$emit('update-settings', localSettings)" />
        </div>
      </div>
    </div>

    <!-- FOV -->
    <div class="tb-item">
      <span>FOV: {{ fovDisplay }}</span>
    </div>

    <!-- Sorting -->
    <div class="tb-item relative">
      <button class="tb-btn" @click="toggleDropdown('sort')">
        Sort Targets ▼
      </button>
      <div class="dropdown-menu" v-if="activeDropdown === 'sort'">
        <div v-for="(opt, key) in sortOptions" :key="key" class="checkbox-row">
             <input type="checkbox" v-model="opt.enabled" @change="updateSort" :id="'sort-'+key">
             <label :for="'sort-'+key">{{ opt.label }}</label>
        </div>
      </div>
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
    min-width: 200px;
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
.small { padding: 5px; font-size: 0.8rem; }
.danger { background-color: #ef4444; border-color: #ef4444; color: white; }

.status-text {
    color: #9ca3af;
    font-size: 0.8rem;
    margin-right: 10px;
}
</style>