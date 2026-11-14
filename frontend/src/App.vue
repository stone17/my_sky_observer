<script setup>
import { ref, onMounted, onUnmounted, watch, computed } from 'vue';
import LeftPanel from './components/LeftPanel.vue';
import ObjectList from './components/ObjectList.vue';
import Framing from './components/Framing.vue';
import AltitudeGraph from './components/AltitudeGraph.vue';
import { computeDefaultImageFov, normalizeImageFov } from './utils/fov.js';
import {
    getAstroNightDefaults,
    resolveEffectiveTimeWindow,
    isTimestampInTimeWindow
} from './utils/timeWindow.js';

const LOCAL_STORAGE_BACKUP_KEY = 'my_sky_observer_settings_backup';

const settings = ref({});
const clientSettings = ref({
    max_magnitude: 12.0,
    min_size: 0.0,
    min_hours: 0.0,
    min_hours_altitude: 30.0,
    selected_types: [],
    filter_active_alt: true,
    filter_na_alt: true,
    filter_active_hours: true,
    filter_na_hours: true,
    filter_active_mag: true,
    filter_na_mag: true,
    filter_active_size: true,
    filter_na_size: true,
    filter_active_time_window: false,
    time_window_start: '',
    time_window_end: '',
    show_object_overlays: true,
    comparison_profiles: [],
    favorites: [],
    filter_favorites_only: false,
    selected_object: '',
    search_query: '',
    object_settings: {}
});
const objects = ref([]);
const selectedObject = ref(null);
const pinnedTopObjectId = ref('');
const pinnedTopSeq = ref(0);
const streamStatus = ref('Idle');
const nightTimes = ref({});
const isDownloading = ref(false);
const downloadProgress = ref("");
const searchQuery = ref("");
const isLoading = ref(false);

// Mobile UI State
const showMobileList = ref(false);
const activeLeftTab = ref('filters');

const handleTabChange = (tab) => {
    activeLeftTab.value = tab;
    if (tab && window.innerWidth <= 1024) {
        showMobileList.value = false;
    }
};

const toggleMobileList = () => {
    showMobileList.value = !showMobileList.value;
    if (showMobileList.value && window.innerWidth <= 1024) {
        activeLeftTab.value = null;
    }
};

const closeMobileList = () => {
    showMobileList.value = false;
};

// --- FOV STATE (Single Source of Truth) ---
const activeFov = ref(1.0);
const isManualFov = ref(false);

const systemFov = computed(() => {
    return computeDefaultImageFov(
        settings.value.telescope,
        settings.value.camera,
        settings.value.image_padding
    );
});

const equipmentSignature = computed(() => {
    const tel = settings.value?.telescope;
    const cam = settings.value?.camera;
    const pad = settings.value?.image_padding ?? 1.15;
    const srv = settings.value?.image_server;
    if (!tel || !cam) return '';
    return `${tel.focal_length}_${cam.sensor_width}_${cam.sensor_height}_${pad}_${srv?.resolution || 1024}_${srv?.source || 'dss2r'}`;
});

// --- CORE DOWNLOAD LOGIC ---
const downloadImage = async (obj, explicitFov = null) => {
    if (!obj) return;

    const targetName = obj.name;
    const requestedFov = normalizeImageFov(
        explicitFov !== null ? explicitFov : (isManualFov.value ? activeFov.value : systemFov.value),
        systemFov.value
    );

    obj.status = 'downloading';

    try {
        const res = await fetch('/api/download-object', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                object: {
                    name: obj.name,
                    ra: obj.ra,
                    dec: obj.dec
                },
                fov: requestedFov,
                settings: {
                    ...settings.value,
                    fov: requestedFov
                }
            })
        });

        if (res.ok) {
            const data = await res.json();
            const effectiveFov = data.image_fov || requestedFov;
            const applyResult = (targetObj) => {
                if (!targetObj) return;
                targetObj.image_url = data.url;
                targetObj.status = data.status || 'cached';
                targetObj.image_fov = effectiveFov;
                if (data.sensor_fov) targetObj.sensor_fov = data.sensor_fov;
                if (data.fov_rectangle) targetObj.fov_rectangle = data.fov_rectangle;
                if (data.setup_hash) targetObj.setup_hash = data.setup_hash;
            };

            applyResult(obj);
            const liveObj = objects.value.find(o => o.name === targetName);
            if (liveObj && liveObj !== obj) {
                applyResult(liveObj);
            }
            if (
                selectedObject.value &&
                selectedObject.value.name === targetName &&
                selectedObject.value !== obj &&
                selectedObject.value !== liveObj
            ) {
                applyResult(selectedObject.value);
            }
            if (isManualFov.value && selectedObject.value?.name === targetName) {
                updateObjectSettingsEntry(targetName, {
                    fov: effectiveFov,
                    is_manual_fov: true,
                    image_url: data.url
                });
            }
        } else {
            obj.status = 'error';
        }
    } catch (e) {
        console.error(e);
        obj.status = 'error';
    }
};

const fetchCustomFramedImageForObject = async (obj, centerRa, centerDec, fov) => {
    if (!obj) return;
    const targetName = obj.name;
    const requestedFov = normalizeImageFov(fov, systemFov.value);
    obj.status = 'downloading';
    try {
        const res = await fetch('/api/fetch-custom-image', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ra: centerRa,
                dec: centerDec,
                fov: requestedFov,
                resolution: settings.value.image_server?.resolution || 1024,
                source: settings.value.image_server?.source || 'dss2r',
                timeout: settings.value.image_server?.timeout || 120
            })
        });
        if (res.ok) {
            const data = await res.json();
            const effectiveFov = data.image_fov || requestedFov;
            const applyCustom = (targetObj) => {
                if (!targetObj) return;
                targetObj.image_url = data.url;
                targetObj.center_ra = centerRa;
                targetObj.center_dec = centerDec;
                targetObj.custom_framed = true;
                targetObj.image_fov = effectiveFov;
                targetObj.status = 'cached';
            };
            applyCustom(obj);
            const liveObj = objects.value.find(o => o.name === targetName);
            if (liveObj && liveObj !== obj) applyCustom(liveObj);
            if (selectedObject.value?.name === targetName && selectedObject.value !== obj) {
                applyCustom(selectedObject.value);
            }
            updateObjectSettingsEntry(targetName, {
                fov: effectiveFov,
                is_manual_fov: Math.abs(effectiveFov - systemFov.value) >= 0.01,
                custom_framed: true,
                center_ra: centerRa,
                center_dec: centerDec,
                image_url: data.url
            });
        } else {
            obj.status = 'error';
        }
    } catch (e) {
        console.error(e);
        obj.status = 'error';
    }
};

// Watcher 1: Equipment / Profile / Padding Changes
watch(equipmentSignature, (newSig, oldSig) => {
    if (!newSig) return;
    if (oldSig && newSig !== oldSig) {
        isManualFov.value = false;
        activeFov.value = systemFov.value;
        if (selectedObject.value) {
            selectedObject.value.custom_framed = false;
            delete selectedObject.value.center_ra;
            delete selectedObject.value.center_dec;
            downloadImage(selectedObject.value, systemFov.value);
        }
    } else if (!isManualFov.value) {
        activeFov.value = systemFov.value;
    }
});

// --- ON-DEMAND OBJECT DETAILS (Altitude / Moon / Sun Graphs) ---
const pendingDetailRequests = new Set();

const mergeObjectDetailsPayload = (d, insertIfMissing = false) => {
    if (!d || !d.name) return null;
    let o = objectMap.get(d.name) || objects.value.find(obj => obj.name === d.name);

    if (!o && insertIfMissing) {
        applySavedStateToObject(d);
        objects.value.push(d);
        o = objects.value[objects.value.length - 1];
        objectMap.set(o.name, o);
    }

    if (o) {
        const targetFov = isManualFov.value ? activeFov.value : systemFov.value;
        const keepCurrentImage =
            o.status === 'downloading' ||
            (o.image_url && o.custom_framed) ||
            (o.image_url && isManualFov.value && Math.abs((o.image_fov || 0) - targetFov) < 0.01) ||
            (o.image_url && !d.image_url && Math.abs((o.image_fov || 0) - (d.image_fov || 0)) < 0.01);

        const savedUrl = o.image_url;
        const savedFov = o.image_fov;
        const savedStatus = o.status;
        const savedCenterRa = o.center_ra;
        const savedCenterDec = o.center_dec;
        const savedCustomFramed = o.custom_framed;

        Object.assign(o, d);
        applySavedStateToObject(o);

        if (keepCurrentImage) {
            o.image_url = savedUrl;
            o.image_fov = savedFov;
            o.status = savedStatus;
            if (savedCenterRa !== undefined) o.center_ra = savedCenterRa;
            if (savedCenterDec !== undefined) o.center_dec = savedCenterDec;
            if (savedCustomFramed !== undefined) o.custom_framed = savedCustomFramed;
        }
    }

    if (selectedObject.value && selectedObject.value.name === d.name) {
        if (o && selectedObject.value !== o) {
            Object.assign(selectedObject.value, o);
            selectedObject.value = o;
        } else if (!o) {
            Object.assign(selectedObject.value, d);
        }
    }
    return o || selectedObject.value;
};

const isValidSingleSessionGraph = (graph, currentNightTimes = nightTimes.value) => {
    if (!Array.isArray(graph) || graph.length < 2) return false;
    const t0 = new Date(graph[0]?.time).getTime();
    const t1 = new Date(graph[graph.length - 1]?.time).getTime();
    if (!Number.isFinite(t0) || !Number.isFinite(t1) || t1 <= t0) return false;
    const spanHours = (t1 - t0) / 3600000;
    if (spanHours > 25.5) return false;

    if (currentNightTimes && typeof currentNightTimes === 'object') {
        const refRange =
            currentNightTimes.night ||
            currentNightTimes.astronomical ||
            currentNightTimes.nautical ||
            currentNightTimes.civil;
        if (Array.isArray(refRange) && refRange.length === 2) {
            const ntStart = new Date(refRange[0]).getTime();
            const ntEnd = new Date(refRange[1]).getTime();
            if (Number.isFinite(ntStart) && Number.isFinite(ntEnd) && ntEnd >= ntStart) {
                const ntMid = 0.5 * (ntStart + ntEnd);
                const toleranceMs = 2 * 3600000;
                if (ntMid < t0 - toleranceMs || ntMid > t1 + toleranceMs) {
                    return false;
                }
            }
        }
    }
    return true;
};

const ensureObjectDetails = async (targetObj) => {
    const objName = targetObj?.name;
    if (!objName) return;
    if (isValidSingleSessionGraph(targetObj.altitude_graph)) return;

    const canonical = objectMap.get(objName);
    if (canonical && isValidSingleSessionGraph(canonical.altitude_graph)) {
        if (targetObj !== canonical) {
            Object.assign(targetObj, canonical);
        }
        return;
    }

    if (pendingDetailRequests.has(objName)) return;
    pendingDetailRequests.add(objName);

    try {
        const qp = new URLSearchParams();
        const loc = settings.value?.location;
        if (loc && loc.latitude !== undefined && loc.longitude !== undefined) {
            qp.set('latitude', String(loc.latitude));
            qp.set('longitude', String(loc.longitude));
        }
        qp.set('tz_offset', String(new Date().getTimezoneOffset()));
        const minHrsAlt = clientSettings.value?.min_hours_altitude ?? settings.value?.min_altitude;
        if (minHrsAlt !== undefined && minHrsAlt !== null) {
            qp.set('min_hours_altitude', String(minHrsAlt));
        }
        const qs = qp.toString();
        const url = `/api/object-details/${encodeURIComponent(objName)}${qs ? `?${qs}` : ''}`;
        const res = await fetch(url);
        if (res.ok) {
            const details = await res.json();
            mergeObjectDetailsPayload(details, true);
        }
    } catch (err) {
        console.error('Failed to fetch object details on demand:', err);
    } finally {
        pendingDetailRequests.delete(objName);
    }
};

const handleExternalSelectObject = (incomingObj) => {
    if (!incomingObj || !incomingObj.name) return;
    let canonical = objectMap.get(incomingObj.name) || objects.value.find(o => o.name === incomingObj.name);
    if (!canonical) {
        canonical = mergeObjectDetailsPayload(incomingObj, true) || incomingObj;
    } else if (
        incomingObj !== canonical &&
        isValidSingleSessionGraph(incomingObj.altitude_graph) &&
        !isValidSingleSessionGraph(canonical.altitude_graph)
    ) {
        Object.assign(canonical, incomingObj);
    }
    pinnedTopObjectId.value = canonical.name;
    pinnedTopSeq.value += 1;
    selectedObject.value = canonical;
    ensureObjectDetails(canonical);
};

const handleListSelectObject = (item) => {
    if (!item) return;
    if (pinnedTopObjectId.value && item.name !== pinnedTopObjectId.value) {
        pinnedTopObjectId.value = '';
    }
    const canonical = objectMap.get(item.name) || item;
    selectedObject.value = canonical;
    ensureObjectDetails(canonical);
};

// Watcher 2: Object Selection
watch(selectedObject, (newVal, oldVal) => {
    if (!newVal) return;
    const isSameObject = newVal?.name === oldVal?.name;

    ensureObjectDetails(newVal);

    if (clientSettings.value.selected_object !== newVal.name) {
        clientSettings.value.selected_object = newVal.name;
        if (!isInitializing) {
            saveSettings({});
        }
    }

    const savedObj = getValidSavedObjectState(newVal.name);
    if (!isSameObject) {
        if (savedObj?.is_manual_fov && savedObj.fov) {
            const normSavedFov = normalizeImageFov(savedObj.fov, systemFov.value);
            isManualFov.value = Math.abs(normSavedFov - systemFov.value) >= 0.01;
            activeFov.value = normSavedFov;
        } else {
            isManualFov.value = false;
            activeFov.value = systemFov.value;
        }
    } else if (!isManualFov.value) {
        activeFov.value = systemFov.value;
    }

    if (savedObj?.rotation !== undefined) {
        newVal.rotation = savedObj.rotation;
    }

    const targetFov = isManualFov.value ? activeFov.value : systemFov.value;

    if (savedObj?.custom_framed && savedObj.center_ra !== undefined && savedObj.center_dec !== undefined) {
        newVal.custom_framed = true;
        newVal.center_ra = savedObj.center_ra;
        newVal.center_dec = savedObj.center_dec;
        const hasCustomImg =
            Boolean(newVal.image_url) &&
            newVal.status === 'cached' &&
            Math.abs((newVal.image_fov || 0) - targetFov) < 0.01;
        if (!hasCustomImg && newVal.status !== 'downloading') {
            fetchCustomFramedImageForObject(newVal, savedObj.center_ra, savedObj.center_dec, targetFov);
        }
    } else {
        const hasMatchingImage =
            Boolean(newVal.image_url) &&
            newVal.status === 'cached' &&
            Math.abs((newVal.image_fov || 0) - targetFov) < 0.01;

        if (
            !hasMatchingImage &&
            newVal.status !== 'downloading' &&
            (!isSameObject || newVal.status !== 'error')
        ) {
            downloadImage(newVal, targetFov);
        }
    }

    if (!isSameObject && window.innerWidth <= 1024) {
        showMobileList.value = false;
    }
});

// Watcher 3: Manual FOV Update from Framing HUD
const handleFovUpdate = (payload) => {
    const isObj = typeof payload === 'object' && payload !== null;
    const rawFov = isObj ? payload.fov : payload;
    const isReset = isObj ? Boolean(payload.isReset) : false;
    const skipDownload = isObj ? Boolean(payload.skipDownload) : false;

    if (isReset) {
        isManualFov.value = false;
        activeFov.value = systemFov.value;
        if (selectedObject.value) {
            const objName = selectedObject.value.name;
            selectedObject.value.custom_framed = false;
            delete selectedObject.value.center_ra;
            delete selectedObject.value.center_dec;
            updateObjectSettingsEntry(objName, {
                fov: null,
                is_manual_fov: false,
                custom_framed: false,
                center_ra: null,
                center_dec: null,
                image_url: null,
                offset_x_pct: 0,
                offset_y_pct: 0
            });
            if (!skipDownload) {
                downloadImage(selectedObject.value, systemFov.value);
            }
        }
        return;
    }

    const normalized = normalizeImageFov(rawFov, systemFov.value);
    isManualFov.value = Math.abs(normalized - systemFov.value) >= 0.01;
    activeFov.value = normalized;

    if (selectedObject.value) {
        updateObjectSettingsEntry(selectedObject.value.name, {
            fov: normalized,
            is_manual_fov: isManualFov.value
        });
        if (!skipDownload) {
            downloadImage(selectedObject.value, normalized);
        }
    }
};

// --- STREAMING & PERSISTENCE LOGIC ---
let eventSource = null;
let restartTimer = null;
let isInitializing = true;
let lastStreamLocationKey = '';
let lastStreamEquipmentSig = '';
let objectMap = new Map();
let saveTimeout = null;
let pendingSavePayload = null;

const getValidSavedObjectState = (objName) => {
    if (!objName) return null;
    const saved = clientSettings.value?.object_settings?.[objName];
    if (!saved || typeof saved !== 'object') return null;
    if (saved.equipment_sig && equipmentSignature.value && saved.equipment_sig !== equipmentSignature.value) {
        return {
            rotation: saved.rotation ?? 0
        };
    }
    return saved;
};

const applySavedStateToObject = (item) => {
    if (!item?.name) return;
    const saved = getValidSavedObjectState(item.name);
    if (!saved) return;
    if (saved.rotation !== undefined) {
        item.rotation = saved.rotation;
    }
    if (saved.custom_framed && saved.center_ra !== undefined && saved.center_ra !== null && saved.center_dec !== undefined && saved.center_dec !== null) {
        item.center_ra = saved.center_ra;
        item.center_dec = saved.center_dec;
        item.custom_framed = true;
    }
    if (saved.is_manual_fov && saved.fov) {
        item.image_fov = normalizeImageFov(saved.fov, systemFov.value);
    }
    if (saved.image_url && (saved.custom_framed || saved.is_manual_fov)) {
        item.image_url = saved.image_url;
        item.status = 'cached';
    }
};

const updateObjectSettingsEntry = (objName, patch) => {
    if (!objName) return;
    if (!clientSettings.value.object_settings || typeof clientSettings.value.object_settings !== 'object') {
        clientSettings.value.object_settings = {};
    }
    const prev = clientSettings.value.object_settings[objName] || {};
    const next = {
        ...prev,
        ...patch,
        equipment_sig: equipmentSignature.value
    };
    Object.keys(next).forEach((k) => {
        if (next[k] === null || next[k] === undefined) {
            delete next[k];
        }
    });
    clientSettings.value.object_settings = {
        ...clientSettings.value.object_settings,
        [objName]: next
    };
    if (!isInitializing) {
        saveSettings({});
    }
};

const handleObjectSettingsUpdate = ({ name, patch }) => {
    if (!name || !patch) return;
    updateObjectSettingsEntry(name, patch);
};

const handleSearchUpdate = (val) => {
    const clean = val ?? '';
    if (clean && pinnedTopObjectId.value) {
        pinnedTopObjectId.value = '';
    }
    searchQuery.value = clean;
    if (clientSettings.value.search_query !== clean) {
        clientSettings.value.search_query = clean;
        if (!isInitializing) {
            saveSettings({});
        }
    }
};

const persistFavoritesImmediate = async (favorites, filterFavoritesOnly) => {
    try {
        await fetch('/api/favorites', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                favorites: Array.isArray(favorites) ? favorites : [],
                filter_favorites_only: Boolean(filterFavoritesOnly)
            }),
            keepalive: true
        });
    } catch (e) {
        console.error(e);
    }
};

const updateClientSettings = (newClientSettings, shouldSave = true) => {
    if (!newClientSettings) return;
    const cloned = JSON.parse(JSON.stringify(newClientSettings));
    const prevFavs = JSON.stringify(clientSettings.value.favorites || []);
    const prevFavFilter = Boolean(clientSettings.value.filter_favorites_only);
    Object.assign(clientSettings.value, cloned);
    if (cloned.search_query !== undefined && searchQuery.value !== cloned.search_query) {
        searchQuery.value = cloned.search_query || '';
    }
    const nextFavs = JSON.stringify(clientSettings.value.favorites || []);
    const nextFavFilter = Boolean(clientSettings.value.filter_favorites_only);
    const favChanged = prevFavs !== nextFavs || prevFavFilter !== nextFavFilter;
    if (shouldSave && !isInitializing) {
        if (favChanged) {
            persistFavoritesImmediate(
                clientSettings.value.favorites,
                clientSettings.value.filter_favorites_only
            );
            saveSettings({}, true);
        } else {
            saveSettings({});
        }
    }
};

const extractEquipmentProfileData = (prof) => {
    const out = {};
    if (prof?.telescope) out.telescope = JSON.parse(JSON.stringify(prof.telescope));
    if (prof?.camera) out.camera = JSON.parse(JSON.stringify(prof.camera));
    if (prof?.image_padding !== undefined) out.image_padding = prof.image_padding;
    return out;
};

const normalizeImageServerConfig = (srv) => {
    const base = srv && typeof srv === 'object' ? srv : {};
    return {
        resolution: Number(base.resolution) || 1024,
        timeout: Number(base.timeout) || 120,
        source: base.source || 'dss2r'
    };
};

const writeBackupToLocalStorage = (payload, pending = false) => {
    try {
        window.localStorage.setItem(
            LOCAL_STORAGE_BACKUP_KEY,
            JSON.stringify({ ts: Date.now(), pending, payload })
        );
    } catch (e) { }
};

const readPendingBackupFromLocalStorage = () => {
    try {
        const raw = window.localStorage.getItem(LOCAL_STORAGE_BACKUP_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (parsed?.pending && parsed?.payload && Date.now() - (parsed.ts || 0) < 30000) {
            return parsed.payload;
        }
    } catch (e) { }
    return null;
};

const flushPendingSave = () => {
    if (!pendingSavePayload) return;
    const payloadToSend = pendingSavePayload;
    pendingSavePayload = null;
    if (saveTimeout) {
        clearTimeout(saveTimeout);
        saveTimeout = null;
    }
    try {
        fetch('/api/settings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payloadToSend),
            keepalive: true
        }).then(() => {
            writeBackupToLocalStorage(payloadToSend, false);
        }).catch(() => {});
    } catch (e) { }
};

const fetchSettings = async () => {
    try {
        const pendingBackup = readPendingBackupFromLocalStorage();
        if (pendingBackup) {
            try {
                await fetch('/api/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(pendingBackup),
                    keepalive: true
                });
                writeBackupToLocalStorage(pendingBackup, false);
            } catch (e) { }
        }

        const res = await fetch('/api/settings');
        if (res.ok) {
            const data = await res.json();
            if (data.active_profile && data.profiles && data.profiles[data.active_profile]) {
                const profileEquipment = extractEquipmentProfileData(data.profiles[data.active_profile]);
                Object.assign(data, profileEquipment);
            }
            data.image_server = normalizeImageServerConfig(data.image_server);
            delete data.catalogs;
            settings.value = data;
            if (data.client_settings) {
                updateClientSettings(data.client_settings, false);
            }
            try {
                const favRes = await fetch('/api/favorites');
                if (favRes.ok) {
                    const favData = await favRes.json();
                    if (Array.isArray(favData?.favorites)) {
                        clientSettings.value.favorites = favData.favorites;
                    }
                    if (favData?.filter_favorites_only !== undefined) {
                        clientSettings.value.filter_favorites_only = Boolean(favData.filter_favorites_only);
                    }
                }
            } catch (e) { }
            searchQuery.value = clientSettings.value.search_query || '';
        }
    } catch (e) { console.error(e); }
};

const saveSettings = (newSettings = {}, immediate = false) => {
    const cleanNew = newSettings ? JSON.parse(JSON.stringify(newSettings)) : {};
    delete cleanNew.client_settings;
    const hasExplicitSettingsUpdate = Object.keys(cleanNew).length > 0;
    const payload = {
        ...settings.value,
        ...cleanNew,
        client_settings: JSON.parse(JSON.stringify(clientSettings.value))
    };
    payload.image_server = normalizeImageServerConfig(payload.image_server);
    settings.value = payload;
    pendingSavePayload = payload;
    writeBackupToLocalStorage(payload, true);

    if (saveTimeout) {
        clearTimeout(saveTimeout);
        saveTimeout = null;
    }

    const doPost = async () => {
        const toSend = pendingSavePayload || payload;
        pendingSavePayload = null;
        saveTimeout = null;
        try {
            const res = await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(toSend),
                keepalive: true
            });
            if (res.ok) {
                writeBackupToLocalStorage(toSend, false);
            }
        } catch (e) { console.error(e); }
    };

    if (immediate || hasExplicitSettingsUpdate) {
        doPost();
    } else {
        saveTimeout = setTimeout(doPost, 150);
    }
};

const buildStreamQueryParams = (mode = 'selected') => {
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
    params.append('sort_key', settings.value.sort_key || 'time');
    params.append('min_altitude', settings.value.min_altitude ?? 30.0);
    params.append('image_padding', settings.value.image_padding || 1.15);
    const effectiveStreamFov = systemFov.value;
    if (effectiveStreamFov > 0) {
        params.append('fov', effectiveStreamFov);
    }

    if (settings.value.image_server) {
        params.append('image_resolution', settings.value.image_server.resolution || 1024);
        params.append('image_timeout', settings.value.image_server.timeout || 120);
        params.append('image_source', settings.value.image_server.source || 'dss2r');
    }

    params.append('download_mode', mode);
    if (clientSettings.value.max_magnitude !== undefined) params.append('max_magnitude', clientSettings.value.max_magnitude);
    if (clientSettings.value.min_size !== undefined) params.append('min_size', clientSettings.value.min_size);
    if (clientSettings.value.min_hours !== undefined) params.append('min_hours', clientSettings.value.min_hours);
    if (clientSettings.value.min_hours_altitude !== undefined) params.append('min_hours_altitude', clientSettings.value.min_hours_altitude);

    if (clientSettings.value.filter_active_alt !== undefined) params.append('filter_active_alt', clientSettings.value.filter_active_alt);
    if (clientSettings.value.filter_na_alt !== undefined) params.append('filter_na_alt', clientSettings.value.filter_na_alt);
    if (clientSettings.value.filter_active_hours !== undefined) params.append('filter_active_hours', clientSettings.value.filter_active_hours);
    if (clientSettings.value.filter_na_hours !== undefined) params.append('filter_na_hours', clientSettings.value.filter_na_hours);
    if (clientSettings.value.filter_active_mag !== undefined) params.append('filter_active_mag', clientSettings.value.filter_active_mag);
    if (clientSettings.value.filter_na_mag !== undefined) params.append('filter_na_mag', clientSettings.value.filter_na_mag);
    if (clientSettings.value.filter_active_size !== undefined) params.append('filter_active_size', clientSettings.value.filter_active_size);
    if (clientSettings.value.filter_na_size !== undefined) params.append('filter_na_size', clientSettings.value.filter_na_size);
    if (clientSettings.value.filter_active_time_window !== undefined) {
        params.append('filter_active_time_window', clientSettings.value.filter_active_time_window);
    }
    if (clientSettings.value.time_window_start) {
        params.append('time_window_start', clientSettings.value.time_window_start);
    }
    if (clientSettings.value.time_window_end) {
        params.append('time_window_end', clientSettings.value.time_window_end);
    }
    params.append('tz_offset', String(new Date().getTimezoneOffset()));

    params.append('selected_types', (clientSettings.value.selected_types || []).join(','));
    if (clientSettings.value.filter_favorites_only !== undefined) {
        params.append('filter_favorites_only', Boolean(clientSettings.value.filter_favorites_only));
    }
    if (Array.isArray(clientSettings.value.favorites) && clientSettings.value.favorites.length > 0) {
        params.append('favorites', clientSettings.value.favorites.join(','));
    }
    return params;
};

let downloadPollTimer = null;

const stopDownloadPolling = () => {
    if (downloadPollTimer) {
        clearInterval(downloadPollTimer);
        downloadPollTimer = null;
    }
};

const applyDownloadObjectUpdate = (s) => {
    if (!s || !s.name) return;
    const o = objectMap.get(s.name) || objects.value.find(obj => obj.name === s.name);
    if (o) {
        const isSelected = selectedObject.value && selectedObject.value.name === o.name;
        if (isSelected && isManualFov.value) {
            o.status = s.status;
        } else {
            o.status = s.status;
            if (s.url) o.image_url = s.url;
            if (s.image_fov) o.image_fov = s.image_fov;
        }
    }
};

const applyDownloadStatus = (statusData) => {
    if (!statusData || typeof statusData !== 'object') return;
    if (Array.isArray(statusData.object_updates)) {
        for (const upd of statusData.object_updates) {
            applyDownloadObjectUpdate(upd);
        }
    }
    if (statusData.active) {
        isDownloading.value = true;
        if (statusData.total > 0) {
            if (statusData.already_cached && statusData.already_cached > 0) {
                downloadProgress.value = `Downloading ${statusData.current} of ${statusData.total} (${statusData.already_cached} cached)`;
            } else {
                downloadProgress.value = `Downloading ${statusData.current} of ${statusData.total}`;
            }
        } else {
            downloadProgress.value = "Starting...";
        }
        ensureDownloadPolling();
    } else {
        isDownloading.value = false;
        downloadProgress.value = "";
        stopDownloadPolling();
    }
};

const pollDownloadStatus = async () => {
    try {
        const res = await fetch('/api/downloads/status');
        if (res.ok) {
            const data = await res.json();
            applyDownloadStatus(data);
        }
    } catch (e) {
        // Ignore transient network errors during poll
    }
};

const ensureDownloadPolling = () => {
    if (!downloadPollTimer) {
        downloadPollTimer = setInterval(pollDownloadStatus, 1000);
    }
};

const startBackgroundDownload = async (mode = 'filtered') => {
    isDownloading.value = true;
    downloadProgress.value = "Starting...";
    const params = buildStreamQueryParams(mode);
    try {
        const res = await fetch(`/api/downloads/start?${params.toString()}`, {
            method: 'POST',
            keepalive: true
        });
        if (res.ok) {
            const data = await res.json();
            applyDownloadStatus(data);
        } else {
            isDownloading.value = false;
            downloadProgress.value = "";
        }
    } catch (e) {
        console.error(e);
        isDownloading.value = false;
        downloadProgress.value = "";
    }
};

const stopBackgroundDownload = async () => {
    stopDownloadPolling();
    isDownloading.value = false;
    downloadProgress.value = "";
    try {
        const res = await fetch('/api/downloads/stop', {
            method: 'POST',
            keepalive: true
        });
        if (res.ok) {
            const data = await res.json();
            applyDownloadStatus(data);
        }
    } catch (e) {
        console.error(e);
    }
};

const startStream = (mode = 'selected') => {
    if (mode === 'filtered' || mode === 'all') {
        startBackgroundDownload(mode);
        return;
    }
    if (restartTimer) {
        clearTimeout(restartTimer);
        restartTimer = null;
    }
    if (eventSource) eventSource.close();
    streamStatus.value = 'Connecting...';
    isLoading.value = true;

    const locKey = settings.value.location
        ? `${settings.value.location.latitude}_${settings.value.location.longitude}`
        : '';
    const equipSig = equipmentSignature.value;
    const params = buildStreamQueryParams('selected');

    eventSource = new EventSource(`/api/stream-objects?${params.toString()}`);

    eventSource.addEventListener('total', (e) => { streamStatus.value = `Found ${e.data} objects.`; });
    eventSource.addEventListener('download_progress', (e) => {
        try {
            const p = JSON.parse(e.data);
            if (p.current !== undefined && p.total !== undefined) {
                isDownloading.value = p.current < p.total;
                if (p.already_cached && p.already_cached > 0) {
                    downloadProgress.value = `Downloading ${p.current} of ${p.total} (${p.already_cached} cached)`;
                } else {
                    downloadProgress.value = `Downloading ${p.current} of ${p.total}`;
                }
                if (isDownloading.value) {
                    ensureDownloadPolling();
                }
            }
        } catch (err) { }
    });
    eventSource.addEventListener('night_times', (e) => {
        try {
            const nt = JSON.parse(e.data);
            nightTimes.value = nt;
            const defaults = getAstroNightDefaults(nt);
            if (!clientSettings.value.time_window_start) {
                clientSettings.value.time_window_start = defaults.start;
            }
            if (!clientSettings.value.time_window_end) {
                clientSettings.value.time_window_end = defaults.end;
            }
        } catch (e) { }
    });

    eventSource.addEventListener('catalog_metadata', (e) => {
        try {
            const newObjects = JSON.parse(e.data);
            const prevSelected = selectedObject.value;
            const currentId = prevSelected?.name || clientSettings.value?.selected_object || '';
            const sameLocation = locKey && locKey === lastStreamLocationKey;
            const sameEquipment = equipSig && equipSig === lastStreamEquipmentSig;
            lastStreamLocationKey = locKey;
            lastStreamEquipmentSig = equipSig;

            const streamTargetFov = systemFov.value;

            for (const item of newObjects) {
                applySavedStateToObject(item);
                const prevObj = objectMap.get(item.name);
                if (prevObj) {
                    if (sameLocation && isValidSingleSessionGraph(prevObj.altitude_graph, nightTimes.value)) {
                        item.altitude_graph = prevObj.altitude_graph;
                        item.moon_graph = prevObj.moon_graph;
                        item.sun_graph = prevObj.sun_graph;
                        if (prevObj.hours_above_min !== undefined) {
                            item.hours_above_min = prevObj.hours_above_min;
                        }
                    }
                    const isPrevSelectedManual =
                        currentId &&
                        item.name === currentId &&
                        (prevObj.custom_framed ||
                            (isManualFov.value &&
                                prevObj.image_url &&
                                Math.abs((prevObj.image_fov || 0) - activeFov.value) < 0.01));
                    if (
                        prevObj.status === 'downloading' ||
                        (sameEquipment && (
                            isPrevSelectedManual ||
                            (prevObj.image_url && Math.abs((prevObj.image_fov || 0) - streamTargetFov) < 0.01)
                        ))
                    ) {
                        item.image_url = prevObj.image_url;
                        item.image_fov = prevObj.image_fov;
                        item.status = prevObj.status;
                        item.center_ra = prevObj.center_ra;
                        item.center_dec = prevObj.center_dec;
                        item.custom_framed = prevObj.custom_framed;
                    }
                }
            }

            objects.value = newObjects;
            const reactiveMap = new Map();
            for (const reactiveItem of objects.value) {
                reactiveMap.set(reactiveItem.name, reactiveItem);
            }
            objectMap = reactiveMap;

            if (currentId) {
                const found = objectMap.get(currentId);
                if (found) {
                    if (
                        sameLocation &&
                        prevSelected &&
                        isValidSingleSessionGraph(prevSelected.altitude_graph, nightTimes.value) &&
                        !found.altitude_graph
                    ) {
                        found.altitude_graph = prevSelected.altitude_graph;
                        found.moon_graph = prevSelected.moon_graph;
                        found.sun_graph = prevSelected.sun_graph;
                    }
                    selectedObject.value = found;
                    ensureObjectDetails(found);
                } else if (prevSelected) {
                    selectedObject.value = null;
                }
            }

            streamStatus.value = `Loaded ${newObjects.length} objects.`;
            isLoading.value = false;
        } catch (err) { }
    });

    eventSource.addEventListener('object_details', (e) => {
        try {
            const d = JSON.parse(e.data);
            mergeObjectDetailsPayload(d, false);
        } catch (err) { }
    });

    eventSource.addEventListener('image_status', (e) => {
        const s = JSON.parse(e.data);
        applyDownloadObjectUpdate(s);
    });

    eventSource.addEventListener('close', () => {
        streamStatus.value = 'Stream Complete';
        isLoading.value = false;
        eventSource.close();
        pollDownloadStatus();
    });
    eventSource.addEventListener('error', () => {
        streamStatus.value = 'Error/Disconnected';
        isLoading.value = false;
        eventSource?.close();
        pollDownloadStatus();
    });
};

const stopStream = () => {
    if (eventSource) {
        eventSource.close();
        eventSource = null;
        streamStatus.value = 'Stopped';
        isLoading.value = false;
    }
};

const handlePurge = () => { objectMap.clear(); objects.value = []; selectedObject.value = null; startStream('selected'); };

const availableTypes = computed(() => {
    const types = new Set();
    objects.value.forEach(o => { if (o.type) types.add(o.type); });
    return Array.from(types).sort();
});

const typeCounts = computed(() => {
    const counts = {};
    objects.value.forEach(o => {
        if (o.type) {
            counts[o.type] = (counts[o.type] || 0) + 1;
        }
    });
    return counts;
});

const typeFilteredObjectsCount = computed(() => {
    const sel = clientSettings.value?.selected_types || [];
    if (sel.length === 0) return objects.value.length;
    if (sel.includes('__NONE__')) return 0;
    const activeSet = new Set(sel);
    return objects.value.filter(o => activeSet.has(o.type)).length;
});

const currentSetupHash = computed(() => {
    for (const o of objects.value) {
        if (o.setup_hash) return o.setup_hash;
    }
    return '';
});

const filteredObjectsList = computed(() => {
    const minAlt = settings.value?.min_altitude || 30;
    const activeAlt = clientSettings.value?.filter_active_alt !== false;
    const naAlt = clientSettings.value?.filter_na_alt !== false;

    const minHrs = clientSettings.value?.min_hours || 0;
    const minHoursAlt = clientSettings.value?.min_hours_altitude ?? minAlt;
    const activeHrs = clientSettings.value?.filter_active_hours !== false;
    const naHrs = clientSettings.value?.filter_na_hours !== false;

    const tw = resolveEffectiveTimeWindow(clientSettings.value || {}, nightTimes.value);

    const maxMag = clientSettings.value?.max_magnitude ?? 12;
    const activeMag = clientSettings.value?.filter_active_mag !== false;
    const naMag = clientSettings.value?.filter_na_mag !== false;

    const minSize = clientSettings.value?.min_size || 0;
    const activeSize = clientSettings.value?.filter_active_size !== false;
    const naSize = clientSettings.value?.filter_na_size !== false;

    const selectedTypes = clientSettings.value?.selected_types || [];
    if (selectedTypes.includes('__NONE__')) return [];
    const activeSet = selectedTypes.length > 0 ? new Set(selectedTypes) : null;

    const onlyFavs = Boolean(clientSettings.value?.filter_favorites_only);
    const favSet = onlyFavs ? new Set(clientSettings.value?.favorites || []) : null;

    const result = [];
    for (const o of objects.value) {
        if (favSet && !favSet.has(o.name)) continue;
        if (activeSet && !activeSet.has(o.type)) continue;

        let effectiveHrs = o.hours_visible;
        if (tw.active && o.altitude_graph && o.altitude_graph.length > 1) {
            const t0 = new Date(o.altitude_graph[0].time).getTime();
            const t1 = new Date(o.altitude_graph[o.altitude_graph.length - 1].time).getTime();
            const spanHours = t1 > t0 ? (t1 - t0) / 3600000 : 24.0;
            const stepHours = spanHours / (o.altitude_graph.length - 1);
            let validCount = 0;
            for (const p of o.altitude_graph) {
                if (p.altitude >= minHoursAlt && isTimestampInTimeWindow(p.time, tw.startMin, tw.endMin)) {
                    validCount++;
                }
            }
            effectiveHrs = Math.round(validCount * stepHours * 10) / 10;
        }

        if (activeHrs) {
            const isNaHrs = effectiveHrs === undefined || effectiveHrs === null;
            if (isNaHrs) {
                if (!naHrs) continue;
            } else if (minHrs > 0 && effectiveHrs < minHrs) {
                continue;
            }
        }

        if (activeAlt) {
            const isNaAlt = o.max_altitude === undefined || o.max_altitude === null;
            if (isNaAlt) {
                if (!naAlt) continue;
            } else if (minAlt > 0 && o.max_altitude < minAlt) {
                continue;
            }
        }

        if (activeMag) {
            const isNaMag = o.mag === undefined || o.mag === null || o.mag >= 99;
            if (isNaMag) {
                if (!naMag) continue;
            } else if (maxMag !== undefined && o.mag > maxMag) {
                continue;
            }
        }

        if (activeSize) {
            const isNaSize = o.maj_ax === undefined || o.maj_ax === null || o.maj_ax === 0;
            if (isNaSize) {
                if (!naSize) continue;
            } else if (minSize > 0 && o.maj_ax < minSize) {
                continue;
            }
        }

        result.push(o);
    }
    return result;
});

const uncachedFilteredCount = computed(() => {
    let count = 0;
    for (const o of filteredObjectsList.value) {
        if (o.status !== 'cached') count++;
    }
    return count;
});

const uncachedAllCount = computed(() => {
    let count = 0;
    for (const o of objects.value) {
        if (o.status !== 'cached') count++;
    }
    return count;
});

const streamParams = computed(() => ({
    loc: settings.value.location,
    tel: settings.value.telescope,
    cam: settings.value.camera,
    min_alt: settings.value.min_altitude,
    min_hrs: clientSettings.value.min_hours,
    min_hrs_alt: clientSettings.value.min_hours_altitude,
    sort: settings.value.sort_key,
    max_mag: clientSettings.value.max_magnitude,
    min_sz: clientSettings.value.min_size,
    fa_alt: clientSettings.value.filter_active_alt,
    fn_alt: clientSettings.value.filter_na_alt,
    fa_hrs: clientSettings.value.filter_active_hours,
    fn_hrs: clientSettings.value.filter_na_hours,
    fa_mag: clientSettings.value.filter_active_mag,
    fn_mag: clientSettings.value.filter_na_mag,
    fa_sz: clientSettings.value.filter_active_size,
    fn_sz: clientSettings.value.filter_na_size,
    fa_tw: clientSettings.value.filter_active_time_window,
    tw_s: clientSettings.value.filter_active_time_window ? clientSettings.value.time_window_start : '',
    tw_e: clientSettings.value.filter_active_time_window ? clientSettings.value.time_window_end : '',
    types: (clientSettings.value.selected_types || []).join(','),
    pad: settings.value.image_padding,
    img_srv: settings.value.image_server,
}));

watch(streamParams, (newVal, oldVal) => {
    if (isInitializing || !oldVal) return;
    const changes = [];
    for (const key in newVal) {
        if (JSON.stringify(newVal[key]) !== JSON.stringify(oldVal[key])) changes.push(key);
    }
    if (changes.length > 0) {
        if (restartTimer) clearTimeout(restartTimer);
        restartTimer = setTimeout(() => { startStream('selected'); }, 1000);
    }
}, { deep: true });

onMounted(async () => {
    window.addEventListener('beforeunload', flushPendingSave);
    window.addEventListener('pagehide', flushPendingSave);
    if (window.innerWidth <= 1024) {
        activeLeftTab.value = null;
    }
    isInitializing = true;
    await fetchSettings();
    startStream('selected');
    await pollDownloadStatus();
    isInitializing = false;
});

onUnmounted(() => {
    window.removeEventListener('beforeunload', flushPendingSave);
    window.removeEventListener('pagehide', flushPendingSave);
    flushPendingSave();
    stopDownloadPolling();
    stopStream();
});

watch(clientSettings, () => {
    if (!isInitializing) saveSettings({});
}, { deep: true });
</script>

<template>
    <div class="app-container">
        <div class="main-layout">
            <!-- Left Expandable Panel (Activity Rail + Drawer) -->
            <LeftPanel
                :settings="settings"
                :client-settings="clientSettings"
                :night-times="nightTimes"
                :available-types="availableTypes"
                :type-counts="typeCounts"
                :stream-status="streamStatus"
                :is-downloading="isDownloading"
                :download-progress="downloadProgress"
                :system-fov="systemFov"
                :selected-object="selectedObject"
                :total-objects-count="typeFilteredObjectsCount"
                :all-catalog-count="objects.length"
                :filtered-count="filteredObjectsList.length"
                :uncached-filtered-count="uncachedFilteredCount"
                :uncached-all-count="uncachedAllCount"
                :setup-hash="currentSetupHash"
                :active-tab="activeLeftTab"
                :show-mobile-list="showMobileList"
                @update-active-tab="handleTabChange"
                @toggle-mobile-list="toggleMobileList"
                @update-settings="saveSettings"
                @update-client-settings="updateClientSettings"
                @start-stream="startStream"
                @stop-stream="stopStream"
                @purge-cache="handlePurge"
                @download-filtered="startBackgroundDownload('filtered')"
                @download-all="startBackgroundDownload('all')"
                @stop-download="stopBackgroundDownload"
            />

            <!-- Center Framing Canvas -->
            <section class="framing-section">
                <div v-if="selectedObject" class="fill-height">
                    <Framing
                        :object="selectedObject"
                        :objects="objects"
                        :settings="settings"
                        :clientSettings="clientSettings"
                        :nightTimes="nightTimes"
                        :searchQuery="searchQuery"
                        :activeFov="activeFov"
                        @update-fov="handleFovUpdate"
                        @update-search="handleSearchUpdate"
                        @select-object="handleExternalSelectObject"
                        @update-settings="saveSettings"
                        @update-client-settings="updateClientSettings"
                        @update-object-settings="handleObjectSettingsUpdate"
                        @open-mobile-list="toggleMobileList"
                    />
                </div>
                <div v-else-if="isLoading" class="empty-state">
                    <div class="spinner"></div>
                    <h2>Loading Targets...</h2>
                    <p>Calculating visibility and altitudes, please wait.</p>
                </div>
                <div v-else class="empty-state">
                    <h2>Select an object to view</h2>
                    <p>Use the navigation panel to configure filters and equipment, or choose a target from the Targets list.</p>
                    <button type="button" class="empty-targets-btn mobile-only-btn" @click="toggleMobileList">
                        📋 Browse Targets ({{ typeFilteredObjectsCount }})
                    </button>
                </div>
            </section>

            <!-- Mobile Backdrop for Right Target Sidebar -->
            <div
                v-if="showMobileList"
                class="sidebar-backdrop"
                @click="closeMobileList"
            ></div>

            <!-- Right Target Sidebar (Altitude Graph + Object List) -->
            <aside class="sidebar" :class="{ 'mobile-visible': showMobileList }">
                <header class="mobile-sidebar-header">
                    <div class="mobile-sidebar-title">
                        <span>📋 Targets & Visibility</span>
                        <span class="mobile-sidebar-badge">{{ typeFilteredObjectsCount }}</span>
                    </div>
                    <button
                        type="button"
                        class="mobile-sidebar-close"
                        @click="closeMobileList"
                        title="Close Targets Panel"
                        aria-label="Close Targets Panel"
                    >
                        ✕
                    </button>
                </header>
                <div class="graph-panel">
                    <AltitudeGraph :object="selectedObject" :location="settings.location" :nightTimes="nightTimes" :settings="settings" :clientSettings="clientSettings" />
                </div>
                <div class="list-panel">
                    <ObjectList
                        :objects="objects"
                        :selectedId="selectedObject?.name"
                        :pinnedId="pinnedTopObjectId"
                        :pinnedSeq="pinnedTopSeq"
                        :settings="settings"
                        :searchQuery="searchQuery"
                        :clientSettings="clientSettings"
                        :nightTimes="nightTimes"
                        :isDownloading="isDownloading"
                        :downloadProgress="downloadProgress"
                        @select="handleListSelectObject"
                        @update-search="handleSearchUpdate"
                        @update-settings="saveSettings"
                        @update-client-settings="updateClientSettings"
                        @fetch-all="startBackgroundDownload('all')"
                        @download-filtered="startBackgroundDownload('filtered')"
                        @stop-download="stopBackgroundDownload"
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

html {
    background-color: var(--bg-color);
}

body {
    margin: 0;
    background-color: var(--bg-color);
    color: var(--text-color);
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    height: 100vh;
    height: 100dvh;
    overflow: hidden;
    overscroll-behavior: none;
}

.app-container {
    display: flex;
    flex-direction: column;
    height: 100vh;
    height: 100dvh;
    width: 100vw;
    overflow: hidden;
}

.main-layout {
    display: flex;
    flex: 1;
    overflow: hidden;
    position: relative;
    height: 100%;
    min-height: 0;
}

.framing-section {
    flex: 1;
    border-right: 1px solid var(--border-color);
    position: relative;
    min-width: 0;
    min-height: 0;
    background: #000;
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
    text-align: center;
    padding: 20px;
    box-sizing: border-box;
}

.empty-targets-btn {
    margin-top: 14px;
    background: #10b981;
    color: #064e3b;
    border: none;
    border-radius: 8px;
    padding: 10px 18px;
    font-size: 0.9rem;
    font-weight: 700;
    cursor: pointer;
}

.mobile-only-btn {
    display: none;
}

.spinner {
    border: 4px solid rgba(255, 255, 255, 0.1);
    width: 40px;
    height: 40px;
    border-radius: 50%;
    border-left-color: #10b981;
    animation: spin 1s linear infinite;
    margin-bottom: 15px;
}

@keyframes spin {
    0% { transform: rotate(0deg); }
    100% { transform: rotate(360deg); }
}

.sidebar {
    width: 410px;
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
    background: var(--bg-color);
    z-index: 10;
}

.mobile-sidebar-header {
    display: none;
}

.sidebar-backdrop {
    display: none;
}

.graph-panel {
    height: 190px;
    border-bottom: 1px solid var(--border-color);
    background: #000;
    flex-shrink: 0;
}

.list-panel {
    flex: 1;
    overflow: hidden;
    min-height: 0;
}

/* Responsive adjustments for tablets and phones */
@media (max-width: 1024px) {
    body {
        overflow: hidden;
    }

    .mobile-only-btn {
        display: inline-flex;
        align-items: center;
        gap: 6px;
    }

    .framing-section {
        border-right: none;
        width: 100%;
        flex: 1 1 100%;
    }

    .sidebar-backdrop {
        display: block;
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.6);
        z-index: 2550;
    }

    .sidebar {
        position: fixed !important;
        top: 0;
        right: 0;
        bottom: 0;
        width: 88%;
        max-width: 400px;
        transform: translateX(100%);
        transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1);
        background: #111827;
        box-shadow: -6px 0 20px rgba(0, 0, 0, 0.65);
        z-index: 2600;
    }

    .sidebar.mobile-visible {
        transform: translateX(0);
    }

    .mobile-sidebar-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 10px 14px;
        background: #1f2937;
        border-bottom: 1px solid #374151;
        flex-shrink: 0;
    }

    .mobile-sidebar-title {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 0.92rem;
        font-weight: 700;
        color: #f3f4f6;
    }

    .mobile-sidebar-badge {
        background: rgba(16, 185, 129, 0.18);
        color: #10b981;
        border: 1px solid rgba(16, 185, 129, 0.35);
        font-size: 0.72rem;
        padding: 1px 7px;
        border-radius: 999px;
    }

    .mobile-sidebar-close {
        background: #374151;
        border: none;
        color: #e5e7eb;
        width: 30px;
        height: 30px;
        border-radius: 6px;
        font-size: 0.95rem;
        display: flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        padding: 0;
        margin: 0;
    }

    .graph-panel {
        height: 150px;
    }
}

/* Phone layout: bottom navigation bar + full-width bottom sheets */
@media (max-width: 768px) {
    .main-layout {
        padding-bottom: calc(56px + env(safe-area-inset-bottom, 0px));
        box-sizing: border-box;
    }

    .sidebar-backdrop {
        bottom: calc(56px + env(safe-area-inset-bottom, 0px));
    }

    .sidebar {
        top: 6vh;
        left: 0;
        right: 0;
        bottom: calc(56px + env(safe-area-inset-bottom, 0px));
        width: 100%;
        max-width: 100%;
        border-top: 1px solid #374151;
        border-radius: 14px 14px 0 0;
        transform: translateY(105%);
        box-shadow: 0 -8px 24px rgba(0, 0, 0, 0.75);
        overflow: hidden;
    }

    .sidebar.mobile-visible {
        transform: translateY(0);
    }

    .graph-panel {
        height: 135px;
    }
}
</style>
