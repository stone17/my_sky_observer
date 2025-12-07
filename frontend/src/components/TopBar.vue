<script setup>
import { ref, onMounted, onUnmounted, watch, computed } from 'vue';

const props = defineProps({
    settings: Object,
    streamStatus: String,
    isDownloading: Boolean,
    downloadProgress: String,
    activeFov: Number // Received from App.vue
});

const emit = defineEmits(['update-settings', 'start-stream', 'stop-stream', 'purge-cache', 'download-filtered', 'download-all', 'stop-download']);

const localSettings = ref({});
const profiles = ref({});
const presets = ref({ cameras: {} });
const cacheStatus = ref({ size_mb: 0, count: 0 });
const selectedProfileName = ref(null);
const newProfileName = ref("");

const activeDropdown = ref(null);

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

            // Sync selected name with settings
            if (localSettings.value.active_profile) {
                selectedProfileName.value = localSettings.value.active_profile;
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

const availableCatalogs = ref([]);
const fetchCatalogs = async () => {
    try {
        const res = await fetch('/api/catalogs');
        if (res.ok) availableCatalogs.value = await res.json();
    } catch (e) { console.error(e); }
};

const selectAllCatalogs = () => {
    localSettings.value.catalogs = [...availableCatalogs.value];
    emit('update-settings', localSettings.value);
};

// Sync settings
watch(() => props.settings, (newVal) => {
    if (newVal) {
        localSettings.value = JSON.parse(JSON.stringify(newVal));
        // Ensure defaults
        if (localSettings.value.min_altitude === undefined) localSettings.value.min_altitude = 30.0;
        if (!localSettings.value.catalogs) localSettings.value.catalogs = ['messier'];
        if (localSettings.value.image_padding === undefined) localSettings.value.image_padding = 1.05;
        if (!localSettings.value.image_server) {
            localSettings.value.image_server = { resolution: 512, timeout: 60, source: 'dss2r' };
        }

        // Sync active profile if present
        if (localSettings.value.active_profile) {
            selectedProfileName.value = localSettings.value.active_profile;
        }
    }
}, { immediate: true, deep: true });

onMounted(() => {
    fetchProfiles();
    fetchPresets();
    fetchCacheStatus();
    fetchCatalogs();
    // Poll cache status every 10s
    const interval = setInterval(fetchCacheStatus, 10000);
    onUnmounted(() => clearInterval(interval));
});

// Actions
const loadProfile = (name) => {
    if (profiles.value[name]) {
        const profileData = JSON.parse(JSON.stringify(profiles.value[name]));

        // Preserve current location if it exists
        const savedLocation = localSettings.value.location;

        // Merge into localSettings
        localSettings.value = {
            ...localSettings.value,
            ...profileData,
            active_profile: name // Persist active profile name
        };

        // Restore location
        if (savedLocation) {
            localSettings.value.location = savedLocation;
        }

        selectedProfileName.value = name;
        emit('update-settings', localSettings.value);
        closeDropdown();
    } else {
        console.error(`DEBUG: Profile '${name}' not found.`);
    }
};

const updateImageSettings = () => {
    emit('update-settings', localSettings.value);
};

const createProfile = async () => {
    if (!newProfileName.value) return;
    const name = newProfileName.value;

    // Create profile data excluding location
    const profileData = JSON.parse(JSON.stringify(localSettings.value));
    delete profileData.location;
    delete profileData.active_profile;
    delete profileData.profiles;

    try {
        await fetch(`/api/profiles/${name}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(profileData)
        });
        await fetchProfiles();

        selectedProfileName.value = name;
        localSettings.value.active_profile = name;
        localSettings.value.profiles = JSON.parse(JSON.stringify(profiles.value));

        emit('update-settings', localSettings.value);
        newProfileName.value = "";
    } catch (e) { console.error(e); }
};

const deleteProfile = async () => {
    if (!selectedProfileName.value) return;
    if (!confirm(`Delete profile "${selectedProfileName.value}"?`)) return;
    try {
        await fetch(`/api/profiles/${selectedProfileName.value}`, { method: 'DELETE' });
        await fetchProfiles();

        if (selectedProfileName.value === localSettings.value.active_profile) {
            localSettings.value.active_profile = null;
        }

        localSettings.value.profiles = JSON.parse(JSON.stringify(profiles.value));

        emit('update-settings', localSettings.value);
        selectedProfileName.value = null;
    } catch (e) { console.error(e); }
};

const applyPreset = (cameraName) => {
    const cam = presets.value.cameras[cameraName];
    if (cam && localSettings.value.camera) {
        localSettings.value.camera.sensor_width = cam.width;
        localSettings.value.camera.sensor_height = cam.height;
        emit('update-settings', localSettings.value);
    }
};

const selectedCameraPreset = computed({
    get() {
        if (!localSettings.value.camera || !presets.value.cameras) return "";
        const w = localSettings.value.camera.sensor_width;
        const h = localSettings.value.camera.sensor_height;
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
    if (!confirm("Are you sure you want to purge the cache? This will delete all downloaded images.")) return;
    await fetch('/api/cache/purge', { method: 'POST' });
    await fetchCacheStatus();
    emit('purge-cache');
};

// City Search
const cityQuery = ref("");
const cityResults = ref([]);
const isSearchingCity = ref(false);

const searchCity = async () => {
    if (!cityQuery.value || cityQuery.value.length < 2) return;
    isSearchingCity.value = true;
    try {
        const res = await fetch(`/api/geocode?city=${encodeURIComponent(cityQuery.value)}`);
        if (res.ok) {
            cityResults.value = await res.json();
        }
    } catch (e) { console.error(e); }
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
    // Just display what App.vue passed down
    if (props.activeFov) return `${props.activeFov.toFixed(2)}°`;
    return "-";
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
        <div class="tb-item relative">
            <button class="tb-btn" @click="toggleDropdown('profile')">
                Profile: {{ selectedProfileName || 'Custom' }} ▼
            </button>
            <div class="dropdown-menu" v-if="activeDropdown === 'profile'">
                <label><strong>Load Profile</strong></label>
                <ul class="link-list">
                    <li v-for="(p, name) in profiles" :key="name" @click="loadProfile(name)">
                        {{ name }} <span v-if="selectedProfileName === name" style="color: #10b981;">✓</span>
                    </li>
                </ul>

                <div class="field-group" style="margin-top: 10px;">
                    <label><strong>New Profile</strong></label>
                    <div style="display: flex; gap: 5px;">
                        <input type="text" v-model="newProfileName" placeholder="Name..." />
                        <button class="small primary" @click="createProfile">Save</button>
                    </div>
                </div>
                <div v-if="selectedProfileName" style="margin-top: 10px;">
                    <button class="full-width danger small" @click="deleteProfile">Delete Current Profile</button>
                </div>

                <hr />
                <div class="field-group" v-if="localSettings.telescope">
                    <label>Focal Length (mm)</label>
                    <input type="number" v-model.number="localSettings.telescope.focal_length"
                        @change="$emit('update-settings', localSettings)" />
                </div>
                <label><strong>Camera Settings</strong></label>
                <select v-model="selectedCameraPreset" class="mini-select">
                    <option value="" disabled>Custom / Select Preset...</option>
                    <option v-for="(specs, name) in presets.cameras" :key="name" :value="name">{{ name }}</option>
                </select>
                <div class="field-group" v-if="localSettings.camera">
                    <label>Sensor Width (mm)</label>
                    <input type="number" step="0.1" v-model.number="localSettings.camera.sensor_width"
                        @change="$emit('update-settings', localSettings)" />
                </div>
                <div class="field-group" v-if="localSettings.camera">
                    <label>Sensor Height (mm)</label>
                    <input type="number" step="0.1" v-model.number="localSettings.camera.sensor_height"
                        @change="$emit('update-settings', localSettings)" />
                </div>
                <div class="field-group">
                    <label>Download Padding (1.0 = Exact FOV)</label>
                    <input type="number" step="0.05" min="0.5" max="2.0" v-model.number="localSettings.image_padding"
                        @change="$emit('update-settings', localSettings)" />
                </div>
            </div>
        </div>

        <div class="tb-item relative">
            <button class="tb-btn" @click="toggleDropdown('location')">
                {{ locationDisplay }} ▼
            </button>
            <div class="dropdown-menu" v-if="activeDropdown === 'location'">
                <div class="field-group" v-if="localSettings.location">
                    <label>Latitude</label>
                    <input type="number" step="0.0001" v-model.number="localSettings.location.latitude"
                        @change="$emit('update-settings', localSettings)" />
                </div>
                <div class="field-group" v-if="localSettings.location">
                    <label>Longitude</label>
                    <input type="number" step="0.0001" v-model.number="localSettings.location.longitude"
                        @change="$emit('update-settings', localSettings)" />
                </div>
                <hr />
                <label><strong>Search City</strong></label>
                <div style="display: flex; gap: 5px;">
                    <input type="text" v-model="cityQuery" @keyup.enter="searchCity" placeholder="City name..."
                        style="flex:1; background: black; border: 1px solid #444; color: white; padding: 5px;" />
                    <button class="small" @click="searchCity">{{ isSearchingCity ? '...' : 'Go' }}</button>
                </div>
                <ul v-if="cityResults.length > 0" class="link-list" style="max-height: 150px; overflow-y: auto;">
                    <li v-for="(city, idx) in cityResults" :key="idx" @click="selectCity(city)">
                        {{ city.name }}, {{ city.country }}
                    </li>
                </ul>
            </div>
        </div>

        <div class="tb-item">
            <span class="status-text" style="color: #e5e7eb; margin:0;">FOV: {{ fovDisplay }}</span>
        </div>

        <div class="tb-item relative">
            <button class="tb-btn" @click="toggleDropdown('catalogs')">
                Catalogs ▼
            </button>
            <div class="dropdown-menu" v-if="activeDropdown === 'catalogs'">
                <label><strong>Active Catalogs</strong></label>
                <div style="margin-bottom: 5px;">
                    <button class="small outline" @click="selectAllCatalogs">Select All</button>
                </div>
                <div class="checkbox-col">
                    <label v-for="cat in availableCatalogs" :key="cat">
                        <input type="checkbox" :value="cat" v-model="localSettings.catalogs"
                            @change="$emit('update-settings', localSettings)">
                        {{ cat }}
                    </label>
                </div>
            </div>
        </div>

        <div class="tb-item relative">
            <button class="tb-btn" @click="toggleDropdown('image_server')">
                Image Server ▼
            </button>
            <div class="dropdown-menu" v-if="activeDropdown === 'image_server'">
                <div class="field-group" v-if="localSettings.image_server">
                    <label><strong>Resolution</strong></label>
                    <select v-model.number="localSettings.image_server.resolution" @change="updateImageSettings"
                        class="mini-select">
                        <option :value="256">256 px</option>
                        <option :value="512">512 px (Default)</option>
                        <option :value="1024">1024 px</option>
                        <option :value="2048">2048 px</option>
                    </select>
                </div>
                <div class="field-group" v-if="localSettings.image_server">
                    <label><strong>Timeout (sec)</strong></label>
                    <input type="number" min="10" max="300" v-model.number="localSettings.image_server.timeout"
                        @change="updateImageSettings" />
                </div>
                <div class="field-group" v-if="localSettings.image_server">
                    <label><strong>Source / Survey</strong></label>
                    <select v-model="localSettings.image_server.source" @change="updateImageSettings"
                        class="mini-select">
                        <option value="dss2r">DSS2 Red (Default)</option>
                        <option value="dss2b">DSS2 Blue</option>
                        <option value="dss2ir">DSS2 IR</option>
                        <option value="sdss">SDSS (Where available)</option>
                    </select>
                </div>
            </div>
        </div>

        <div class="tb-item" v-if="!isDownloading">
            <button class="tb-btn" @click="$emit('download-filtered')">Download Filtered</button>
            <button class="tb-btn" @click="$emit('download-all')">Download All</button>
        </div>
        <div class="tb-item" v-else>
            <button class="tb-btn danger" @click="$emit('stop-download')">Stop Downloading</button>
            <span class="status-text" style="margin-left: 10px;">{{ downloadProgress }}</span>
        </div>

        <div class="tb-item relative push-right">
            <button class="tb-btn" @click="toggleDropdown('cache')">
                Cache: {{ cacheStatus.size_mb }} MB ▼
            </button>
            <div class="dropdown-menu right-aligned" v-if="activeDropdown === 'cache'">
                <div class="info-row">Images: {{ cacheStatus.count }}</div>
                <hr />
                <button class="full-width danger" @click="purgeCache">Purge Cache</button>
            </div>
        </div>

        <div class="tb-item">
            <span class="status-text">{{ streamStatus }}</span>
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
    background: rgba(255, 255, 255, 0.1);
    border-radius: 4px;
}

.push-right {
    margin-left: auto;
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
    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
}

.dropdown-menu.right-aligned {
    left: auto;
    right: 0;
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

.full-width {
    width: 100%;
}

.small {
    padding: 2px 8px;
    font-size: 0.8rem;
}

.danger {
    background-color: #ef4444;
    border-color: #ef4444;
    color: white;
}

.secondary {
    background-color: #4b5563;
    border-color: #4b5563;
    color: white;
}

.primary {
    background-color: #10b981;
    border-color: #10b981;
    color: #000;
    font-weight: bold;
}

.status-text {
    color: #9ca3af;
    font-size: 0.8rem;
    margin-right: 10px;
}

.radio-group {
    display: flex;
    flex-direction: column;
    gap: 5px;
    margin-bottom: 10px;
}

.radio-group label {
    display: flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
    color: #e5e7eb;
}

.checkbox-col {
    display: flex;
    flex-direction: column;
    gap: 5px;
    margin-bottom: 5px;
}

.checkbox-col label {
    display: flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
    color: #e5e7eb;
}
</style>