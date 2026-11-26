<script setup>
import { computed, ref, watch, nextTick, onMounted, onUnmounted } from 'vue';
import {
    getObjectTypeLabel,
    getObjectTypeShort,
    getObjectTypeDescription,
    formatMagnitude
} from '../utils/catalogMeta.js';
import {
    resolveEffectiveTimeWindow
} from '../utils/timeWindow.js';
import {
    parseObserverLocation,
    computeCatalogVisibilityHours,
    buildSessionSparklineBasis,
    computeObjectSparklinePath,
    doesGraphMatchEvening
} from '../utils/observingTime.js';
import { computeDefaultImageFov, normalizeImageFov } from '../utils/fov.js';
import {
    skyAtlasReady,
    skyAtlasVersion,
    ensureSkyAtlasLoaded,
    getAtlasThumbnailSnapshot
} from '../utils/skyAtlas.js';
import { openEphemerisModal } from '../utils/skyViewOptions.js';

const props = defineProps([
    'objects',
    'solarObjects',
    'catalogTrigTable',
    'observingEvening',
    'selectedId',
    'activeFov',
    'pinnedId',
    'pinnedSeq',
    'settings',
    'clientSettings',
    'nightTimes',
    'searchQuery',
    'isDownloading',
    'downloadProgress',
    'downloadDetails'
]);
const emit = defineEmits([
    'select',
    'enter-frame',
    'fetch-all',
    'download-filtered',
    'stop-download',
    'dismiss-download-summary',
    'update-settings',
    'update-client-settings',
    'update-search'
]);

const listDownloadPercent = computed(() => {
    const total = Number(props.downloadDetails?.total || 0);
    const current = Number(props.downloadDetails?.current || 0);
    if (total <= 0) return 0;
    return Math.min(100, Math.max(0, Math.round((current / total) * 100)));
});

const stopBtnLabel = computed(() => {
    const d = props.downloadDetails;
    if (d && Number(d.total) > 0) {
        return `${d.current ?? 0}/${d.total}`;
    }
    if (props.downloadProgress) {
        return props.downloadProgress.replace(/^Downloading\s*/i, '').replace(/\s*—.*$/, '').replace(/\s*\(.*\)/, '') || 'Stop';
    }
    return 'Stop';
});

const activeDownloadTargets = computed(() => {
    const list = props.downloadDetails?.active_targets;
    return Array.isArray(list) ? list : [];
});

const showPostDownloadBanner = computed(() => {
    if (props.isDownloading) return false;
    const d = props.downloadDetails;
    if (!d?.last_summary) return false;
    return Number(d.failed) > 0 || Number(d.total) === 0 || Boolean(d.last_error);
});

const getStatusTooltip = (item) => {
    if (!item) return '';
    if (item.status === 'live') {
        return item.solar_id === 'sun'
            ? 'Live solar ephemeris (daytime altitude arc)'
            : 'Live topocentric ephemeris at active observing time';
    }
    if (item.status === 'error') {
        return item.error_message ? `Download failed: ${item.error_message}` : 'Survey download failed';
    }
    if (item.status === 'downloading') {
        const active = activeDownloadTargets.value.find((t) => t.id === item.name);
        if (active) {
            return `Downloading (${Math.round(Number(active.elapsed_s) || 0)}s) — ${active.detail || 'Fetching from SkyView'}`;
        }
        return 'Downloading survey image…';
    }
    if (item.status === 'cached') return 'Survey image cached locally';
    return 'Pending survey image';
};

const showInfoModal = ref(false);
const infoObject = ref(null);
const showSuggestions = ref(false);
const scrollableListRef = ref(null);

const localSettings = ref({});
const localClientSettings = ref({});
const renderedLimit = ref(50);

const sortOptions = [
    { value: 'time', label: 'Peak Altitude' },
    { value: 'hours_above', label: 'Visible Hours' },
    { value: 'brightness', label: 'Brightness (Mag)' },
    { value: 'size', label: 'Angular Size' }
];

watch(() => props.settings, (newVal) => {
    if (newVal && JSON.stringify(newVal) !== JSON.stringify(localSettings.value)) {
        localSettings.value = JSON.parse(JSON.stringify(newVal));
        if (localSettings.value.min_altitude === undefined) localSettings.value.min_altitude = 30.0;
        if (!localSettings.value.sort_key) localSettings.value.sort_key = 'time';
    }
}, { immediate: true, deep: true });

watch(() => props.clientSettings, (newVal) => {
    if (newVal) {
        localClientSettings.value = { ...newVal };
        if (localClientSettings.value.filter_active_alt === undefined) localClientSettings.value.filter_active_alt = true;
        if (localClientSettings.value.filter_na_alt === undefined) localClientSettings.value.filter_na_alt = true;
        if (localClientSettings.value.filter_active_hours === undefined) localClientSettings.value.filter_active_hours = true;
        if (localClientSettings.value.filter_na_hours === undefined) localClientSettings.value.filter_na_hours = true;
        if (localClientSettings.value.min_hours_altitude === undefined) {
            localClientSettings.value.min_hours_altitude = localSettings.value.min_altitude ?? 30.0;
        }
        if (localClientSettings.value.filter_active_mag === undefined) localClientSettings.value.filter_active_mag = true;
        if (localClientSettings.value.filter_na_mag === undefined) localClientSettings.value.filter_na_mag = true;
        if (localClientSettings.value.filter_active_size === undefined) localClientSettings.value.filter_active_size = true;
        if (localClientSettings.value.filter_na_size === undefined) localClientSettings.value.filter_na_size = true;
        if (!Array.isArray(localClientSettings.value.solar_categories)) {
            localClientSettings.value.solar_categories = ['sun_moon', 'planets', 'minor'];
        }
        if (localClientSettings.value.solar_filter_alt === undefined) {
            localClientSettings.value.solar_filter_alt = false;
        }
        if (!['dso', 'solar', 'favorites'].includes(localClientSettings.value.target_view_mode)) {
            localClientSettings.value.target_view_mode = localClientSettings.value.filter_favorites_only
                ? 'favorites'
                : 'dso';
        }
    }
}, { immediate: true, deep: true });

const updateSettings = () => emit('update-settings', { sort_key: localSettings.value.sort_key });

const favoritesSet = computed(() => {
    const list = localClientSettings.value?.favorites;
    return new Set(Array.isArray(list) ? list : []);
});

const favoritesCount = computed(() => favoritesSet.value.size);

const activeViewMode = computed(() => {
    const m = localClientSettings.value?.target_view_mode;
    if (m === 'solar' || m === 'favorites' || m === 'dso') return m;
    return localClientSettings.value?.filter_favorites_only ? 'favorites' : 'dso';
});

const isFavoritesFilterActive = computed(() => {
    return activeViewMode.value === 'favorites';
});

const setViewMode = (mode) => {
    if (!['dso', 'solar', 'favorites'].includes(mode)) return;
    const updated = {
        ...localClientSettings.value,
        target_view_mode: mode,
        filter_favorites_only: mode === 'favorites'
    };
    localClientSettings.value = updated;
    renderedLimit.value = 50;
    scrollListToTop();
    emit('update-client-settings', updated);
};

const isFavorite = (name) => {
    return Boolean(name && favoritesSet.value.has(name));
};

const toggleFavorite = (name) => {
    if (!name) return;
    const next = new Set(favoritesSet.value);
    if (next.has(name)) {
        next.delete(name);
    } else {
        next.add(name);
    }
    const updated = {
        ...localClientSettings.value,
        favorites: Array.from(next)
    };
    localClientSettings.value = updated;
    emit('update-client-settings', updated);
};

const toggleFavoritesFilter = () => {
    setViewMode(activeViewMode.value === 'favorites' ? 'dso' : 'favorites');
};

const scrollListToTop = () => {
    nextTick(() => {
        if (scrollableListRef.value) {
            scrollableListRef.value.scrollTop = 0;
        }
    });
};

watch(() => props.pinnedSeq, () => {
    if (props.pinnedId) {
        scrollListToTop();
    }
});

watch(() => props.selectedId, (newId) => {
    if (newId) {
        if (props.pinnedId && newId === props.pinnedId) {
            scrollListToTop();
            return;
        }
        const idx = filteredAndSortedObjects.value.findIndex(o => o.name === newId);
        if (idx >= renderedLimit.value) {
            renderedLimit.value = idx + 20;
        }
        nextTick(() => {
            const el = document.getElementById(`obj-card-${newId}`);
            if (el) el.scrollIntoView({ block: 'nearest' });
        });
    }
});

const openInfo = (obj) => {
    infoObject.value = obj;
    showInfoModal.value = true;
};

const onSearchInput = (e) => {
    emit('update-search', e.target.value);
    showSuggestions.value = true;
};

const clearSearch = () => {
    emit('update-search', '');
    showSuggestions.value = false;
};

const selectSuggestion = (obj) => {
    if (obj?.is_solar_system && activeViewMode.value === 'dso') {
        setViewMode('solar');
    } else if (obj && !obj.is_solar_system && activeViewMode.value === 'solar') {
        setViewMode('dso');
    }
    emit('update-search', obj.name);
    handleObjectClick(obj);
    showSuggestions.value = false;
};

const onSearchBlur = () => {
    setTimeout(() => { showSuggestions.value = false; }, 200);
};

const ensureNormSearchFields = (o) => {
    if (!o) return;
    if (o._normId === undefined) o._normId = (o.name || '').toLowerCase().replace(/\s+/g, '');
    if (o._normCommon === undefined) o._normCommon = (o.common_name || '').toLowerCase().replace(/\s+/g, '');
    if (o._normOther === undefined) o._normOther = (o.other_id || '').toLowerCase().replace(/\s+/g, '');
    if (o._normAliases === undefined) o._normAliases = (o.aliases || '').toLowerCase().replace(/\s+/g, '');
    if (o._normAliasTokens === undefined) {
        o._normAliasTokens = (o.aliases || '')
            .split(',')
            .map(t => t.toLowerCase().replace(/\s+/g, ''))
            .filter(Boolean);
    }
};

const suggestionList = computed(() => {
    const q = (props.searchQuery || '').trim();
    if (!q) return [];
    const normQ = q.toLowerCase().replace(/\s+/g, '');
    const pool =
        activeViewMode.value === 'solar'
            ? [...(props.solarObjects || []), ...(props.objects || [])]
            : [...(props.solarObjects || []), ...(props.objects || [])];
    const matches = pool.filter(o => {
        ensureNormSearchFields(o);
        return (
            o._normId.includes(normQ) ||
            o._normCommon.includes(normQ) ||
            o._normOther.includes(normQ) ||
            o._normAliases.includes(normQ)
        );
    });
    return matches.slice(0, 8);
});

const observerLocation = computed(() => parseObserverLocation(props.settings));

const catalogIndexByName = computed(() => {
    const list = props.objects || [];
    const map = new Map();
    for (let i = 0; i < list.length; i++) {
        const name = list[i]?.name;
        if (name) map.set(name, i);
    }
    return map;
});

const effectiveTimeWindow = computed(() =>
    resolveEffectiveTimeWindow(localClientSettings.value, props.nightTimes)
);

const visibilityHoursArr = computed(() => {
    const minAlt = localSettings.value.min_altitude || 30;
    const minHoursAlt = localClientSettings.value.min_hours_altitude ?? minAlt;
    return computeCatalogVisibilityHours({
        trigTable: props.catalogTrigTable,
        evening: props.observingEvening,
        observerLocation: observerLocation.value,
        minAltitudeDeg: minHoursAlt,
        timeWindow: effectiveTimeWindow.value
    });
});

const getItemHours = (item) => {
    if (!item) return 0;
    if (item.is_solar_system) {
        return item.hours_visible ? Math.round(item.hours_visible * 10) / 10 : 0;
    }
    const arr = visibilityHoursArr.value;
    if (arr) {
        const idx = catalogIndexByName.value.get(item.name);
        if (idx !== undefined && idx < arr.length) {
            const v = arr[idx];
            if (v >= 0) return Math.round(v * 10) / 10;
            return 0;
        }
    }
    return item.hours_visible ? Math.round(item.hours_visible * 10) / 10 : 0;
};

const filteredState = computed(() => {
    const dsoList = props.objects || [];
    const solarList = props.solarObjects || [];
    const mode = activeViewMode.value;

    const minAlt = localSettings.value.min_altitude || 30;
    const activeAlt = localClientSettings.value.filter_active_alt !== false;
    const naAlt = localClientSettings.value.filter_na_alt !== false;

    const minHrs = localClientSettings.value.min_hours || 0;
    const activeHrs = localClientSettings.value.filter_active_hours !== false;
    const naHrs = localClientSettings.value.filter_na_hours !== false;

    const maxMag = localClientSettings.value.max_magnitude ?? 12;
    const activeMag = localClientSettings.value.filter_active_mag !== false;
    const naMag = localClientSettings.value.filter_na_mag !== false;

    const minSize = localClientSettings.value.min_size || 0;
    const activeSize = localClientSettings.value.filter_active_size !== false;
    const naSize = localClientSettings.value.filter_na_size !== false;

    const selectedTypes = localClientSettings.value.selected_types || [];
    const rejectAllTypes = selectedTypes.includes('__NONE__');
    const selectedTypeSet =
        !rejectAllTypes && selectedTypes.length > 0 ? new Set(selectedTypes) : null;

    const solarCategories = Array.isArray(localClientSettings.value.solar_categories)
        ? new Set(localClientSettings.value.solar_categories)
        : new Set(['sun_moon', 'planets', 'minor']);
    const solarFilterAlt = Boolean(localClientSettings.value.solar_filter_alt);

    const sortKey = localSettings.value.sort_key || 'time';

    const q = (props.searchQuery || '').trim().toLowerCase();
    const normQ = q.replace(/\s+/g, '');

    const favSet = favoritesSet.value;
    const pinnedName = props.pinnedId || '';

    const visHours = visibilityHoursArr.value;
    const searchMatches = new Set();
    const result = [];
    const resultHours = sortKey === 'hours_above' ? new Map() : null;

    const checkSearchMatch = (o) => {
        if (!normQ) return false;
        ensureNormSearchFields(o);
        if (
            o._normId.includes(normQ) ||
            o._normCommon.includes(normQ) ||
            o._normOther.includes(normQ) ||
            o._normAliases.includes(normQ)
        ) {
            searchMatches.add(o.name);
            return true;
        }
        return false;
    };

    // 1. Solar System items (in 'solar' mode, 'favorites' mode, or if pinned from Sky Map)
    if (mode === 'solar' || mode === 'favorites' || pinnedName) {
        for (let i = 0; i < solarList.length; i++) {
            const s = solarList[i];
            const isPinned = Boolean(pinnedName && s.name === pinnedName);
            if (mode === 'dso' && !isPinned) continue;
            if (mode === 'favorites' && !isPinned && !favSet.has(s.name)) continue;

            const isMatch = checkSearchMatch(s);
            if (!isMatch && !isPinned) {
                if (mode === 'solar') {
                    const cat = s.solar_category || 'planets';
                    if (!solarCategories.has(cat)) continue;
                }
                if (solarFilterAlt && minAlt > 0 && (s.max_altitude ?? 0) < minAlt) {
                    continue;
                }
            }

            if (resultHours) {
                resultHours.set(s.name, s.hours_visible || 0);
            }
            result.push(s);
        }
    }

    // 2. Deep-Sky Catalog items (in 'dso' or 'favorites' mode)
    if (mode === 'dso' || mode === 'favorites') {
        const len = dsoList.length;
        for (let i = 0; i < len; i++) {
            const o = dsoList[i];
            const isPinned = Boolean(pinnedName && o.name === pinnedName);
            if (mode === 'favorites' && !isPinned && !favSet.has(o.name)) continue;

            const isMatch = checkSearchMatch(o);

            const rawHrs = visHours && i < visHours.length ? visHours[i] : o.hours_visible;
            const isNaHrs = rawHrs === undefined || rawHrs === null || rawHrs < 0;
            const effectiveHrs = isNaHrs ? 0 : rawHrs;

            // If it's not a search match or Sky-Map-pinned target, apply normal filters.
            if (!isMatch && !isPinned) {
                if (rejectAllTypes) continue;
                if (selectedTypeSet && !selectedTypeSet.has(o.type)) continue;

                if (activeHrs) {
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
            }

            if (resultHours) {
                resultHours.set(o.name, effectiveHrs);
            }
            result.push(o);
        }
    }

    result.sort((a, b) => {
        const aPinned = Boolean(pinnedName && a.name === pinnedName);
        const bPinned = Boolean(pinnedName && b.name === pinnedName);
        if (aPinned && !bPinned) return -1;
        if (!aPinned && bPinned) return 1;

        if (normQ) {
            const aMatch = searchMatches.has(a.name);
            const bMatch = searchMatches.has(b.name);
            if (aMatch && !bMatch) return -1;
            if (!aMatch && bMatch) return 1;

            if (aMatch && bMatch) {
                const normA = a._normId || '';
                const normB = b._normId || '';

                const exactA = normA === normQ || (a._normAliasTokens && a._normAliasTokens.includes(normQ));
                const exactB = normB === normQ || (b._normAliasTokens && b._normAliasTokens.includes(normQ));
                if (exactA && !exactB) return -1;
                if (exactB && !exactA) return 1;

                const aStarts = normA.startsWith(normQ);
                const bStarts = normB.startsWith(normQ);
                if (aStarts && !bStarts) return -1;
                if (bStarts && !aStarts) return 1;

                if (normA.length !== normB.length) return normA.length - normB.length;
                return normA.localeCompare(normB);
            }
        }

        let valA, valB, dir = -1;

        if (sortKey === 'time' || sortKey === 'altitude') {
            valA = a.max_altitude || 0;
            valB = b.max_altitude || 0;
        } else if (sortKey === 'hours_above') {
            valA = resultHours ? (resultHours.get(a.name) || 0) : 0;
            valB = resultHours ? (resultHours.get(b.name) || 0) : 0;
        } else if (sortKey === 'brightness') {
            valA = a.mag !== undefined && a.mag !== null ? a.mag : 99;
            valB = b.mag !== undefined && b.mag !== null ? b.mag : 99;
            dir = 1;
        } else if (sortKey === 'size') {
            valA = a.maj_ax || 0;
            valB = b.maj_ax || 0;
        }

        if (valA !== valB) return (valA - valB) * dir;
        return 0;
    });

    return { list: result, searchMatches };
});

const filteredAndSortedObjects = computed(() => filteredState.value.list);
const isItemSearchMatch = (item) => Boolean(item && filteredState.value.searchMatches.has(item.name));

const displayedObjects = computed(() => {
    return filteredAndSortedObjects.value.slice(0, renderedLimit.value);
});

const typePoolCount = computed(() => {
    if (activeViewMode.value === 'solar') {
        const cats = Array.isArray(localClientSettings.value.solar_categories)
            ? new Set(localClientSettings.value.solar_categories)
            : new Set(['sun_moon', 'planets', 'minor']);
        return (props.solarObjects || []).filter((s) => cats.has(s.solar_category || 'planets')).length;
    }
    if (activeViewMode.value === 'favorites') {
        return favoritesCount.value;
    }
    const selectedTypes = localClientSettings.value.selected_types || [];
    if (selectedTypes.length === 0) return (props.objects || []).length;
    if (selectedTypes.includes('__NONE__')) return 0;
    const activeSet = new Set(selectedTypes);
    return (props.objects || []).filter(o => activeSet.has(o.type)).length;
});

const uncachedFilteredCount = computed(() => {
    let count = 0;
    for (const o of filteredAndSortedObjects.value) {
        if (!o.is_solar_system && o.status !== 'cached') {
            count++;
        }
    }
    return count;
});

watch(filteredAndSortedObjects, (newList) => {
    const savedId = props.selectedId || localClientSettings.value?.selected_object;
    const savedIdx = savedId ? newList.findIndex(o => o.name === savedId) : -1;
    renderedLimit.value = savedIdx >= 50 ? savedIdx + 20 : 50;
    if (!props.selectedId && newList.length > 0) {
        if (savedIdx !== -1) {
            handleObjectClick(newList[savedIdx]);
        } else {
            const savedInAll = savedId
                ? (props.solarObjects || []).find(o => o.name === savedId) ||
                  (props.objects || []).find(o => o.name === savedId)
                : null;
            handleObjectClick(savedInAll || newList[0]);
        }
    }
}, { immediate: true });

// --- LAZY FOV-AWARE SKY ATLAS THUMBNAILS ---
// Uses a single IntersectionObserver on .scrollable-list + a frame-budgeted idle queue
// (max 3 thumbnails / 3.0ms per slice) so scrolling 13k targets stays locked at 60fps.
const atlasThumbUrls = ref({});
const atlasThumbMeta = new Map(); // name -> { key: string, complete: boolean }
const cardElByName = new Map();
const visibleCardNames = new Set();
const displayedItemByName = new Map();

const pendingThumbQueue = [];
const pendingThumbSet = new Set();
let cardObserver = null;
let thumbPumpHandle = null;
let thumbPumpIsIdleCb = false;
let isActivelyScrolling = false;
let scrollIdleTimer = null;

const defaultSetupFov = computed(() => {
    return computeDefaultImageFov(
        props.settings?.telescope,
        props.settings?.camera,
        props.settings?.image_padding
    );
});

const equipmentSig = computed(() => {
    const fl = Number(props.settings?.telescope?.focal_length) || 0;
    const sw = Number(props.settings?.camera?.sensor_width) || 0;
    const sh = Number(props.settings?.camera?.sensor_height) || 0;
    if (!fl || !sw || !sh) return '';
    return `${fl.toFixed(2)}_${sw.toFixed(2)}_${sh.toFixed(2)}`;
});

const resolveItemThumbParams = (item) => {
    if (!item) return null;
    const rawSaved = localClientSettings.value?.object_settings?.[item.name];
    const saved =
        rawSaved && (!rawSaved.equipment_sig || !equipmentSig.value || rawSaved.equipment_sig === equipmentSig.value)
            ? rawSaved
            : null;

    let fov = defaultSetupFov.value;
    if (item.name === props.selectedId && Number(props.activeFov) > 0) {
        fov = normalizeImageFov(props.activeFov, defaultSetupFov.value);
    } else if (item.image_fov !== undefined && item.image_fov !== null && Number(item.image_fov) > 0) {
        fov = normalizeImageFov(item.image_fov, defaultSetupFov.value);
    } else if (saved?.is_manual_fov && Number(saved?.fov) > 0) {
        fov = normalizeImageFov(saved.fov, defaultSetupFov.value);
    }

    let ra = Number(item.ra);
    let dec = Number(item.dec);
    if (item.custom_framed && Number.isFinite(Number(item.center_ra)) && Number.isFinite(Number(item.center_dec))) {
        ra = Number(item.center_ra);
        dec = Number(item.center_dec);
    } else if (saved?.custom_framed && Number.isFinite(Number(saved?.center_ra)) && Number.isFinite(Number(saved?.center_dec))) {
        ra = Number(saved.center_ra);
        dec = Number(saved.center_dec);
    }

    if (!Number.isFinite(ra) || !Number.isFinite(dec)) return null;
    return { fov, ra, dec };
};

const getServerThumbUrl = (item) => {
    if (!item?.image_url) return null;
    const params = resolveItemThumbParams(item);
    if (!params) return item.image_url;
    if (item.image_fov !== undefined && item.image_fov !== null) {
        if (Math.abs(Number(item.image_fov) - params.fov) >= 0.03) {
            return null;
        }
    }
    return item.image_url;
};

const cancelThumbPump = () => {
    if (thumbPumpHandle === null) return;
    if (thumbPumpIsIdleCb && typeof window !== 'undefined' && typeof window.cancelIdleCallback === 'function') {
        window.cancelIdleCallback(thumbPumpHandle);
    } else if (typeof cancelAnimationFrame === 'function') {
        cancelAnimationFrame(thumbPumpHandle);
    }
    thumbPumpHandle = null;
    thumbPumpIsIdleCb = false;
};

const scheduleThumbPump = () => {
    if (thumbPumpHandle !== null || pendingThumbQueue.length === 0 || !skyAtlasReady.value) return;
    if (typeof window !== 'undefined' && typeof window.requestIdleCallback === 'function') {
        thumbPumpIsIdleCb = true;
        thumbPumpHandle = window.requestIdleCallback(processThumbQueueSlice, { timeout: 90 });
    } else if (typeof requestAnimationFrame === 'function') {
        thumbPumpIsIdleCb = false;
        thumbPumpHandle = requestAnimationFrame(() => processThumbQueueSlice(null));
    }
};

const processThumbQueueSlice = (deadline) => {
    thumbPumpHandle = null;
    thumbPumpIsIdleCb = false;
    if (!skyAtlasReady.value || pendingThumbQueue.length === 0) return;

    const startMs = typeof performance !== 'undefined' ? performance.now() : Date.now();
    const allowDetail = !isActivelyScrolling;
    let processed = 0;
    let changed = false;
    const batch = {};

    while (pendingThumbQueue.length > 0 && processed < 3) {
        if (processed > 0) {
            const elapsed = (typeof performance !== 'undefined' ? performance.now() : Date.now()) - startMs;
            if (elapsed >= 3.0) break;
            if (deadline && typeof deadline.timeRemaining === 'function' && !deadline.didTimeout && deadline.timeRemaining() < 2) {
                break;
            }
        }

        const name = pendingThumbQueue.shift();
        pendingThumbSet.delete(name);
        if (!name || !visibleCardNames.has(name)) continue;

        const item = displayedItemByName.get(name);
        if (!item || getServerThumbUrl(item)) continue;

        const params = resolveItemThumbParams(item);
        if (!params) continue;

        const coordKey = `${params.ra.toFixed(3)}_${params.dec.toFixed(3)}_${params.fov.toFixed(2)}`;
        const prevMeta = atlasThumbMeta.get(name);
        if (prevMeta && prevMeta.key === coordKey && (prevMeta.complete || !allowDetail) && atlasThumbUrls.value[name]) {
            continue;
        }

        const snap = getAtlasThumbnailSnapshot(item.name, params.ra, params.dec, params.fov, {
            allowLoadDetail: allowDetail,
            primaryTarget: item.name
        });
        processed++;

        if (snap && snap.url) {
            atlasThumbMeta.set(name, { key: coordKey, complete: Boolean(snap.complete) });
            if (atlasThumbUrls.value[name] !== snap.url) {
                batch[name] = snap.url;
                changed = true;
            }
        }
    }

    if (changed) {
        const merged = { ...atlasThumbUrls.value, ...batch };
        const keys = Object.keys(merged);
        if (keys.length > 420) {
            let toRemove = keys.length - 360;
            for (let i = 0; i < keys.length && toRemove > 0; i++) {
                const k = keys[i];
                if (!visibleCardNames.has(k) && k !== props.selectedId) {
                    delete merged[k];
                    atlasThumbMeta.delete(k);
                    toRemove--;
                }
            }
        }
        atlasThumbUrls.value = merged;
    }

    if (pendingThumbQueue.length > 0) {
        scheduleThumbPump();
    }
};

const enqueueThumbWork = (name, highPriority = false) => {
    if (!name || !skyAtlasReady.value) return;
    const item = displayedItemByName.get(name);
    if (!item || getServerThumbUrl(item)) return;

    const params = resolveItemThumbParams(item);
    if (!params) return;
    const coordKey = `${params.ra.toFixed(3)}_${params.dec.toFixed(3)}_${params.fov.toFixed(2)}`;
    const prevMeta = atlasThumbMeta.get(name);
    if (prevMeta && prevMeta.key === coordKey && (prevMeta.complete || isActivelyScrolling) && atlasThumbUrls.value[name]) {
        return;
    }

    if (pendingThumbSet.has(name)) {
        if (highPriority) {
            const idx = pendingThumbQueue.indexOf(name);
            if (idx > 0) {
                pendingThumbQueue.splice(idx, 1);
                pendingThumbQueue.unshift(name);
            }
        }
        return;
    }

    pendingThumbSet.add(name);
    if (highPriority) {
        pendingThumbQueue.unshift(name);
    } else {
        pendingThumbQueue.push(name);
    }
    scheduleThumbPump();
};

const enqueueVisibleThumbs = () => {
    if (!skyAtlasReady.value) return;
    for (const name of visibleCardNames) {
        enqueueThumbWork(name, name === props.selectedId);
    }
};

const setCardRef = (el, name) => {
    const prev = cardElByName.get(name);
    if (prev && prev !== el) {
        if (cardObserver) cardObserver.unobserve(prev);
        cardElByName.delete(name);
        visibleCardNames.delete(name);
    }
    if (el) {
        cardElByName.set(name, el);
        if (cardObserver) cardObserver.observe(el);
    }
};

const initCardObserver = () => {
    if (cardObserver) {
        cardObserver.disconnect();
        cardObserver = null;
    }
    if (typeof IntersectionObserver === 'undefined') return;
    cardObserver = new IntersectionObserver(
        (entries) => {
            for (let i = 0; i < entries.length; i++) {
                const entry = entries[i];
                const name = entry.target?.dataset?.objName;
                if (!name) continue;
                if (entry.isIntersecting) {
                    visibleCardNames.add(name);
                    enqueueThumbWork(name, name === props.selectedId);
                } else {
                    visibleCardNames.delete(name);
                }
            }
        },
        {
            root: scrollableListRef.value || null,
            rootMargin: '160px 0px',
            threshold: 0.01
        }
    );
    for (const el of cardElByName.values()) {
        cardObserver.observe(el);
    }
};

watch(
    displayedObjects,
    (list) => {
        displayedItemByName.clear();
        for (let i = 0; i < list.length; i++) {
            const item = list[i];
            if (item?.name) displayedItemByName.set(item.name, item);
        }
        nextTick(() => {
            enqueueVisibleThumbs();
        });
    },
    { immediate: true }
);

watch([skyAtlasReady, skyAtlasVersion], () => {
    enqueueVisibleThumbs();
});

watch(defaultSetupFov, () => {
    enqueueVisibleThumbs();
});

watch(
    () => [props.selectedId, props.activeFov],
    ([selId]) => {
        if (selId && visibleCardNames.has(selId)) {
            enqueueThumbWork(selId, true);
        }
    }
);

const onScroll = (e) => {
    isActivelyScrolling = true;
    if (scrollIdleTimer) clearTimeout(scrollIdleTimer);
    scrollIdleTimer = setTimeout(() => {
        scrollIdleTimer = null;
        isActivelyScrolling = false;
        enqueueVisibleThumbs();
    }, 180);

    const { scrollTop, scrollHeight, clientHeight } = e.target;
    if (scrollTop + clientHeight >= scrollHeight - 300) {
        if (renderedLimit.value < filteredAndSortedObjects.value.length) {
            renderedLimit.value += 50;
        }
    }
};

const handleObjectClick = (item) => {
    if (!item) return;
    emit('select', item);
};

const handleObjectDoubleClick = (item) => {
    if (!item) return;
    emit('enter-frame', item);
};

const miniThresholdY = computed(() => {
    const alt = Math.max(0, Math.min(90, Number(localClientSettings.value.min_hours_altitude ?? localSettings.value.min_altitude ?? 30)));
    return Math.round((40 - (alt / 90) * 40) * 10) / 10;
});

const sparklineBasis = computed(() =>
    buildSessionSparklineBasis(props.observingEvening, observerLocation.value, 24)
);

const getAltitudePath = (altitudeGraph) => {
    if (!altitudeGraph || altitudeGraph.length < 2) return "";
    const width = 100;
    const height = 40;
    const maxAlt = 90;
    const stepX = width / (altitudeGraph.length - 1);
    let d = `M 0 ${height}`;
    altitudeGraph.forEach((point, index) => {
        const x = index * stepX;
        const y = height - (Math.max(0, point.altitude) / maxAlt) * height;
        d += ` L ${x} ${y}`;
    });
    d += ` L ${width} ${height} Z`;
    return d;
};

const getMiniSparklinePath = (item) => {
    if (!item) return "";
    if (item.is_solar_system && Array.isArray(item.altitude_graph) && item.altitude_graph.length > 1) {
        return getAltitudePath(item.altitude_graph);
    }
    if (sparklineBasis.value && Number.isFinite(item.ra) && Number.isFinite(item.dec)) {
        return computeObjectSparklinePath(item, sparklineBasis.value, 100, 40);
    }
    if (
        item.altitude_graph &&
        item.altitude_graph.length > 1 &&
        (!props.observingEvening?.key || doesGraphMatchEvening(item.altitude_graph, props.observingEvening.key))
    ) {
        return getAltitudePath(item.altitude_graph);
    }
    return "";
};

const buildLunarPhaseThumbPath = (phaseFraction = 0.5, isWaxing = true) => {
    const r = 13;
    const k = Math.max(0.0, Math.min(1.0, Number(phaseFraction) || 0));
    if (k <= 0.01) return '';
    if (k >= 0.99) {
        return `M 18,${18 - r} A ${r},${r} 0 1,1 18,${18 + r} A ${r},${r} 0 1,1 18,${18 - r} Z`;
    }
    const rx = Math.abs(1.0 - 2.0 * k) * r;
    // Waxing: sunlit limb on right; Waning: sunlit limb on left
    const outerSweep = isWaxing ? 1 : 0;
    const innerSweep = k > 0.5 ? outerSweep : 1 - outerSweep;
    return `M 18,${18 - r} A ${r},${r} 0 0,${outerSweep} 18,${18 + r} A ${rx.toFixed(2)},${r} 0 0,${innerSweep} 18,${18 - r} Z`;
};

const handleKeydown = (e) => {
    const list = filteredAndSortedObjects.value;
    if (list.length === 0) return;
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;

    if (e.key === 'ArrowDown') {
        const idx = list.findIndex(o => o.name === props.selectedId);
        if (idx < list.length - 1) handleObjectClick(list[idx + 1]);
        else if (idx === -1) handleObjectClick(list[0]);
        e.preventDefault();
    } else if (e.key === 'ArrowUp') {
        const idx = list.findIndex(o => o.name === props.selectedId);
        if (idx > 0) handleObjectClick(list[idx - 1]);
        e.preventDefault();
    }
};

onMounted(() => {
    window.addEventListener('keydown', handleKeydown);
    ensureSkyAtlasLoaded();
    nextTick(() => {
        initCardObserver();
    });
});

onUnmounted(() => {
    window.removeEventListener('keydown', handleKeydown);
    cancelThumbPump();
    if (scrollIdleTimer) {
        clearTimeout(scrollIdleTimer);
        scrollIdleTimer = null;
    }
    if (cardObserver) {
        cardObserver.disconnect();
        cardObserver = null;
    }
    cardElByName.clear();
    visibleCardNames.clear();
});
</script>

<template>
    <div class="list-wrapper">
        <div class="list-content">
            <!-- Unified Mode Switcher + Search + Sort + Count Header -->
            <div class="list-header">
                <div class="list-mode-tabs" role="tablist" aria-label="Target List View">
                    <button
                        type="button"
                        role="tab"
                        class="list-mode-tab"
                        :class="{ active: activeViewMode === 'dso' }"
                        :aria-selected="activeViewMode === 'dso'"
                        @click="setViewMode('dso')"
                        title="Deep-Sky Catalog Targets"
                    >
                        <span>✨ Deep Sky</span>
                    </button>
                    <button
                        type="button"
                        role="tab"
                        class="list-mode-tab solar"
                        :class="{ active: activeViewMode === 'solar' }"
                        :aria-selected="activeViewMode === 'solar'"
                        @click="setViewMode('solar')"
                        title="Sun, Moon, Planets &amp; Saved JPL Ephemerides"
                    >
                        <span>🪐 Solar</span>
                        <span class="mode-pill-count">{{ (solarObjects || []).length }}</span>
                    </button>
                    <button
                        type="button"
                        role="tab"
                        class="list-mode-tab favs"
                        :class="{ active: activeViewMode === 'favorites' }"
                        :aria-selected="activeViewMode === 'favorites'"
                        @click="setViewMode('favorites')"
                        title="Saved Favorites across Deep Sky &amp; Solar System"
                    >
                        <span>{{ activeViewMode === 'favorites' ? '★' : '☆' }} Favs</span>
                        <span class="mode-pill-count">{{ favoritesCount }}</span>
                    </button>
                </div>

                <div class="search-box">
                    <span class="search-icon">🔍</span>
                    <input
                        type="text"
                        :value="searchQuery"
                        @input="onSearchInput"
                        @focus="showSuggestions = true"
                        @blur="onSearchBlur"
                        :placeholder="
                            activeViewMode === 'solar'
                                ? 'Search solar objects (Mars, Moon, 12P...)'
                                : 'Search targets (name, M31, Jupiter...)'
                        "
                        class="search-input"
                    />
                    <button
                        v-if="searchQuery"
                        type="button"
                        class="clear-search-btn"
                        @click="clearSearch"
                        title="Clear search"
                    >
                        ✕
                    </button>

                    <div v-if="showSuggestions && suggestionList.length > 0" class="suggestions-popover">
                        <div
                            v-for="s in suggestionList"
                            :key="`${s.is_solar_system ? 'sol-' : ''}${s.name}`"
                            class="suggestion-item"
                            @mousedown.prevent="selectSuggestion(s)"
                            @touchstart.passive="selectSuggestion(s)"
                        >
                            <div class="sug-main">
                                <span v-if="s.is_solar_system" class="sug-solar-sym">{{ s.symbol || '🪐' }}</span>
                                <strong>{{ s.name }}</strong>
                                <span v-if="s.aliases" class="sug-aliases">({{ s.aliases }})</span>
                            </div>
                            <span v-if="s.common_name && s.common_name !== 'N/A'" class="sug-common">
                                {{ s.common_name }}
                            </span>
                        </div>
                    </div>
                </div>

                <div class="toolbar-row">
                    <div class="sort-control">
                        <label for="sort-select">Sort:</label>
                        <select id="sort-select" v-model="localSettings.sort_key" @change="updateSettings" class="sort-select">
                            <option v-for="opt in sortOptions" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                        </select>
                    </div>

                    <div class="toolbar-right">
                        <div class="list-stats" title="Matching targets / Total targets in active view">
                            <strong>{{ filteredAndSortedObjects.length }}</strong> / {{ typePoolCount }}
                        </div>

                        <button
                            v-if="activeViewMode === 'solar'"
                            type="button"
                            class="toolbar-dl-btn solar-add-btn"
                            @click="openEphemerisModal()"
                            title="Search &amp; download Comet, Asteroid, or Spacecraft from NASA JPL Horizons"
                        >
                            ☄ + Add Object
                        </button>
                        <button
                            v-else-if="!isDownloading"
                            type="button"
                            class="toolbar-dl-btn"
                            :class="{ 'all-cached': filteredAndSortedObjects.length > 0 && uncachedFilteredCount === 0 }"
                            :disabled="uncachedFilteredCount === 0"
                            @click="emit('download-filtered')"
                            :title="
                                uncachedFilteredCount > 0
                                    ? `Download ${uncachedFilteredCount} remaining sky survey images`
                                    : filteredAndSortedObjects.length > 0
                                      ? `All matching deep-sky targets are already downloaded`
                                      : 'No matching targets to download'
                            "
                        >
                            <template v-if="filteredAndSortedObjects.length > 0 && uncachedFilteredCount === 0">
                                ✓ Cached ({{ filteredAndSortedObjects.length }})
                            </template>
                            <template v-else>
                                ⬇️ Download ({{ uncachedFilteredCount }})
                            </template>
                        </button>
                        <button
                            v-else
                            type="button"
                            class="toolbar-dl-btn stop"
                            @click="emit('stop-download')"
                            :title="downloadProgress ? `${downloadProgress} — Click to stop` : 'Stop downloading images'"
                        >
                            🛑 {{ stopBtnLabel }}
                        </button>
                    </div>
                </div>

                <!-- Compact Live Batch Download Status Strip -->
                <div v-if="isDownloading" class="list-dl-strip">
                    <div class="list-dl-strip-top">
                        <span class="list-dl-spinner" aria-hidden="true"></span>
                        <span class="list-dl-text">
                            <template v-if="activeDownloadTargets.length > 0">
                                <strong>{{ activeDownloadTargets.map((t) => `${t.id} (${Math.round(Number(t.elapsed_s) || 0)}s)`).join(', ') }}</strong>
                                <span v-if="activeDownloadTargets[0]?.detail" class="list-dl-sub"> · {{ activeDownloadTargets[0].detail }}</span>
                            </template>
                            <template v-else>
                                {{ downloadProgress || 'Preparing download queue…' }}
                            </template>
                        </span>
                        <span v-if="Number(downloadDetails?.failed) > 0" class="list-dl-fail-pill" :title="downloadDetails?.last_error || ''">
                            ✗ {{ downloadDetails.failed }}
                        </span>
                    </div>
                    <div class="list-dl-bar">
                        <div
                            class="list-dl-bar-fill"
                            :class="{ indeterminate: !downloadDetails?.total || listDownloadPercent === 0 }"
                            :style="downloadDetails?.total ? { width: `${listDownloadPercent}%` } : {}"
                        ></div>
                    </div>
                </div>
                <div v-else-if="showPostDownloadBanner" class="list-dl-strip summary" :class="{ error: Number(downloadDetails?.failed) > 0 || Boolean(downloadDetails?.last_error) }">
                    <div class="list-dl-strip-top">
                        <span class="list-dl-text" :title="downloadDetails?.last_error || downloadDetails?.last_summary">
                            {{ downloadDetails?.last_summary }}
                            <template v-if="downloadDetails?.last_error"> — {{ downloadDetails.last_error }}</template>
                        </span>
                        <button
                            type="button"
                            class="list-dl-dismiss"
                            @click="emit('dismiss-download-summary')"
                            title="Dismiss"
                        >
                            ✕
                        </button>
                    </div>
                </div>
            </div>

            <!-- Scrollable Target Cards -->
            <div ref="scrollableListRef" class="scrollable-list" @scroll="onScroll">
                <div
                    v-for="item in displayedObjects"
                    :key="item.name"
                    :id="`obj-card-${item.name}`"
                    :ref="(el) => setCardRef(el, item.name)"
                    :data-obj-name="item.name"
                    class="object-card"
                    :class="{ active: selectedId === item.name, 'search-match': isItemSearchMatch(item) }"
                    @click="handleObjectClick(item)"
                    @dblclick="handleObjectDoubleClick(item)"
                >
                    <div class="card-content">
                        <div class="img-col">
                            <img
                                v-if="getServerThumbUrl(item)"
                                :src="getServerThumbUrl(item)"
                                class="list-thumb"
                                loading="lazy"
                                decoding="async"
                            />
                            <div
                                v-else-if="item.is_solar_system"
                                class="solar-thumb-card"
                                :title="`${item.name} — Live Solar System Target`"
                            >
                                <img
                                    v-if="atlasThumbUrls[item.name]"
                                    :src="atlasThumbUrls[item.name]"
                                    class="list-thumb solar-bg-atlas"
                                    decoding="async"
                                />
                                <svg class="solar-emblem-svg" viewBox="0 0 36 36" aria-hidden="true">
                                    <template v-if="item.solar_id === 'sun'">
                                        <circle cx="18" cy="18" r="15" fill="rgba(251, 191, 36, 0.22)" />
                                        <circle cx="18" cy="18" r="10.5" fill="#fde047" stroke="#f59e0b" stroke-width="1.2" />
                                    </template>
                                    <template v-else-if="item.solar_id === 'moon'">
                                        <circle cx="18" cy="18" r="13" fill="#1e293b" stroke="rgba(226, 232, 240, 0.45)" stroke-width="1" />
                                        <path
                                            v-if="buildLunarPhaseThumbPath(item.phase_fraction, item.is_waxing)"
                                            :d="buildLunarPhaseThumbPath(item.phase_fraction, item.is_waxing)"
                                            fill="#f8fafc"
                                        />
                                    </template>
                                    <template v-else-if="item.solar_id === 'saturn'">
                                        <ellipse cx="18" cy="18" rx="15.5" ry="5.5" transform="rotate(-16 18 18)" fill="none" stroke="#fde68a" stroke-width="2" />
                                        <circle cx="18" cy="18" r="7.5" :fill="item.solar_color || '#fde68a'" />
                                    </template>
                                    <template v-else-if="item.is_ephemeris">
                                        <line x1="28" y1="8" x2="15" y2="21" :stroke="item.solar_color || '#34d399'" stroke-width="3.5" stroke-linecap="round" opacity="0.45" />
                                        <circle cx="14" cy="22" r="5" :fill="item.solar_color || '#34d399'" />
                                    </template>
                                    <template v-else>
                                        <circle cx="18" cy="18" r="12" :fill="item.solar_color || '#fbbf24'" opacity="0.2" />
                                        <circle cx="18" cy="18" r="8.5" :fill="item.solar_color || '#fbbf24'" />
                                    </template>
                                </svg>
                            </div>
                            <div
                                v-else-if="atlasThumbUrls[item.name]"
                                class="atlas-thumb-wrap"
                                title="Live FOV snapshot from Sky Atlas"
                            >
                                <img
                                    :src="atlasThumbUrls[item.name]"
                                    class="list-thumb atlas-live-thumb"
                                    decoding="async"
                                />
                                <span class="atlas-thumb-dot" aria-hidden="true"></span>
                            </div>
                            <div v-else class="img-placeholder">
                                <span>✦</span>
                            </div>
                        </div>

                        <div class="info-col">
                            <div class="title-row">
                                <button
                                    type="button"
                                    class="card-fav-btn"
                                    :class="{ 'is-fav': isFavorite(item.name) }"
                                    @click.stop="toggleFavorite(item.name)"
                                    @dblclick.stop
                                    :title="isFavorite(item.name) ? 'Remove from Favorites' : 'Save to Favorites'"
                                    :aria-label="isFavorite(item.name) ? 'Remove from Favorites' : 'Save to Favorites'"
                                >
                                    {{ isFavorite(item.name) ? '★' : '☆' }}
                                </button>
                                <h4 :title="item.name">{{ item.name }}</h4>
                                <span
                                    v-if="item.type"
                                    class="type-tag"
                                    :title="`${getObjectTypeLabel(item.type)} (${item.type}) — ${getObjectTypeDescription(item.type)}`"
                                >
                                    {{ getObjectTypeShort(item.type) }}
                                </span>
                                <button type="button" class="info-icon" @click.stop="openInfo(item)" @dblclick.stop title="Target Details">ⓘ</button>

                                <div class="status-indicator" :class="`status-${item.status}`" :title="getStatusTooltip(item)">
                                    <span v-if="item.status === 'live'">●</span>
                                    <span v-else-if="item.status === 'cached'">✓</span>
                                    <span v-else-if="item.status === 'downloading'" class="spin-glyph">⟳</span>
                                    <span v-else-if="item.status === 'error'">✗</span>
                                    <span v-else>⋯</span>
                                </div>
                            </div>

                            <div class="common-name" :title="item.common_name && item.common_name !== 'N/A' ? item.common_name + ' • ' + item.constellation : item.constellation">
                                <template v-if="item.common_name && item.common_name !== 'N/A'">
                                    {{ item.common_name }} &bull;
                                </template>
                                {{ item.constellation }}
                            </div>

                            <div
                                v-if="item.aliases"
                                class="aliases-line"
                                :title="`Same-names / collated IDs: ${item.aliases}`"
                            >
                                ≡ {{ item.aliases }}
                            </div>

                            <div class="metrics-row">
                                <span>Mag: <strong>{{ formatMagnitude(item.mag) }}</strong></span>
                                <span>Size: <strong>{{ item.size }}</strong></span>
                                <span class="vis-time">Vis: {{ getItemHours(item) }}h</span>
                            </div>
                        </div>

                        <div class="graph-col">
                            <div class="altitude-chart" v-if="getMiniSparklinePath(item)">
                                <svg viewBox="0 0 100 40" preserveAspectRatio="none" width="100%" height="40">
                                    <line x1="0" :y1="miniThresholdY" x2="100" :y2="miniThresholdY" stroke="#374151" stroke-width="1" stroke-dasharray="2" />
                                    <path
                                        :d="getMiniSparklinePath(item)"
                                        :fill="item.solar_id === 'sun' ? 'rgba(245, 158, 11, 0.28)' : 'rgba(16, 185, 129, 0.25)'"
                                        :stroke="item.solar_id === 'sun' ? '#f59e0b' : '#10b981'"
                                        stroke-width="1.2"
                                    />
                                </svg>
                            </div>
                            <div class="coord-label">
                                {{ typeof item.ra === 'number' ? item.ra.toFixed(1) : item.ra }}° / {{ typeof item.dec === 'number' ? item.dec.toFixed(1) : item.dec }}°
                            </div>
                        </div>
                    </div>
                </div>

                <div v-if="filteredAndSortedObjects.length === 0" class="empty-list">
                    <p v-if="isFavoritesFilterActive && favoritesCount === 0">
                        No favorites saved yet. Click ☆ on any Deep Sky or Solar System target to bookmark it.
                    </p>
                    <p v-else-if="isFavoritesFilterActive">
                        None of your {{ favoritesCount }} saved favorites match the current filters.
                    </p>
                    <p v-else-if="activeViewMode === 'solar'">
                        No Solar System objects match the selected categories or altitude filter.
                    </p>
                    <p v-else>No targets match your current filters.</p>
                </div>
            </div>
        </div>

        <!-- Info Modal -->
        <div v-if="showInfoModal" class="modal-overlay" @click="showInfoModal = false">
            <div class="modal-content" @click.stop>
                <h3>{{ infoObject?.is_solar_system ? `${infoObject.symbol || ''} ${infoObject.name}`.trim() : (infoObject?.common_name && infoObject?.common_name !== 'N/A' ? infoObject.common_name : infoObject?.name) }}</h3>
                <p v-if="!infoObject?.is_solar_system && infoObject?.common_name && infoObject?.common_name !== 'N/A'"><strong>Catalog Name:</strong> {{ infoObject.name }}</p>
                <p v-if="infoObject?.is_solar_system && infoObject?.common_name"><strong>Summary:</strong> {{ infoObject.common_name }}</p>
                <p v-if="infoObject?.aliases"><strong>Also Known As:</strong> {{ infoObject.aliases }}</p>
                <p v-if="infoObject?.sub_objects && infoObject.sub_objects.length > 0">
                    <strong>Collated in Field ({{ infoObject.sub_objects.length }}):</strong>
                    {{ infoObject.sub_objects.map(s => s.name).join(', ') }}
                </p>
                <p><strong>RA/DEC:</strong> {{ typeof infoObject?.ra === 'number' ? infoObject.ra.toFixed(3) : infoObject?.ra }} / {{ typeof infoObject?.dec === 'number' ? infoObject.dec.toFixed(3) : infoObject?.dec }}</p>
                <p><strong>Type:</strong> {{ getObjectTypeLabel(infoObject?.type) }} <small v-if="infoObject?.type">({{ infoObject.type }})</small></p>
                <p><strong>Constellation:</strong> {{ infoObject?.constellation || 'N/A' }}</p>
                <p><strong>Magnitude:</strong> {{ formatMagnitude(infoObject?.mag) }}</p>
                <p><strong>{{ infoObject?.is_ephemeris ? 'Distance:' : 'Angular Size:' }}</strong> {{ infoObject?.size }}</p>
                <p v-if="infoObject?.is_solar_system && infoObject?.distance_au"><strong>Distance:</strong> {{ Number(infoObject.distance_au).toFixed(4) }} AU</p>
                <p v-if="infoObject?.is_solar_system && !infoObject?.is_ephemeris && infoObject?.solar_id !== 'sun'">
                    <strong>Illumination:</strong> {{ Math.round(Number(infoObject.phase_percent ?? 100)) }}%<template v-if="infoObject.phase_name"> ({{ infoObject.phase_name }})</template>
                </p>
                <p v-if="!infoObject?.is_solar_system"><strong>Surface Brightness:</strong> {{ infoObject?.surface_brightness }}</p>
                <button @click="showInfoModal = false" class="close-btn">Close</button>
            </div>
        </div>
    </div>
</template>

<style scoped>
.list-wrapper {
    display: flex;
    flex-direction: column;
    height: 100%;
    overflow: hidden;
    background: #111827;
}

.list-content {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
}

.list-header {
    background: #1f2937;
    border-bottom: 1px solid #374151;
    padding: 7px 10px 8px;
    display: flex;
    flex-direction: column;
    gap: 7px;
    z-index: 50;
}

.list-mode-tabs {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 4px;
    background: #111827;
    padding: 3px;
    border-radius: 7px;
    border: 1px solid #374151;
}

.list-mode-tab {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
    padding: 4px 6px;
    border-radius: 5px;
    border: 1px solid transparent;
    background: transparent;
    color: #9ca3af;
    font-size: 0.74rem;
    font-weight: 600;
    cursor: pointer;
    margin: 0;
    white-space: nowrap;
    transition: all 0.15s ease;
}

.list-mode-tab:hover {
    color: #f3f4f6;
    background: rgba(55, 65, 81, 0.55);
}

.list-mode-tab.active {
    background: rgba(16, 185, 129, 0.18);
    border-color: rgba(16, 185, 129, 0.55);
    color: #34d399;
}

.list-mode-tab.solar.active {
    background: rgba(245, 158, 11, 0.18);
    border-color: rgba(245, 158, 11, 0.55);
    color: #fbbf24;
}

.list-mode-tab.favs.active {
    background: rgba(251, 191, 36, 0.18);
    border-color: rgba(251, 191, 36, 0.6);
    color: #fde047;
}

.mode-pill-count {
    font-size: 0.65rem;
    padding: 0 5px;
    border-radius: 999px;
    background: rgba(17, 24, 39, 0.75);
    color: inherit;
    font-weight: 700;
}

.sug-solar-sym {
    font-size: 0.85rem;
    flex-shrink: 0;
}

.toolbar-dl-btn.solar-add-btn {
    background: rgba(56, 189, 248, 0.14);
    color: #7dd3fc;
    border-color: rgba(56, 189, 248, 0.45);
}

.toolbar-dl-btn.solar-add-btn:hover {
    background: #0284c7;
    color: #f8fafc;
    border-color: #0284c7;
}

.solar-thumb-card {
    position: relative;
    width: 100%;
    height: 100%;
    background: radial-gradient(circle at 50% 50%, #0f172a 0%, #030712 100%);
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
}

.solar-bg-atlas {
    position: absolute;
    inset: 0;
    opacity: 0.45;
    pointer-events: none;
}

.solar-emblem-svg {
    position: relative;
    z-index: 2;
    width: 38px;
    height: 38px;
    filter: drop-shadow(0 2px 5px rgba(0, 0, 0, 0.65));
}

.search-box {
    position: relative;
    display: flex;
    align-items: center;
    background: #111827;
    border: 1px solid #4b5563;
    border-radius: 6px;
    padding: 0 8px;
    height: 34px;
}

.search-box:focus-within {
    border-color: #10b981;
}

.search-icon {
    font-size: 0.8rem;
    margin-right: 6px;
    opacity: 0.7;
}

.search-input {
    flex: 1;
    background: transparent !important;
    border: none !important;
    color: #f3f4f6 !important;
    font-size: 0.85rem;
    padding: 0 !important;
    margin: 0 !important;
    height: 100% !important;
    outline: none !important;
    box-shadow: none !important;
}

.clear-search-btn {
    background: transparent;
    border: none;
    color: #9ca3af;
    cursor: pointer;
    font-size: 0.85rem;
    padding: 2px 4px;
    margin: 0;
}

.clear-search-btn:hover {
    color: #fff;
}

.suggestions-popover {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    right: 0;
    background: #1f2937;
    border: 1px solid #4b5563;
    border-radius: 6px;
    max-height: 260px;
    overflow-y: auto;
    z-index: 100;
    box-shadow: 0 8px 16px rgba(0, 0, 0, 0.5);
}

.suggestion-item {
    padding: 7px 10px;
    cursor: pointer;
    font-size: 0.82rem;
    border-bottom: 1px solid #374151;
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.suggestion-item:hover {
    background: #374151;
}

.sug-main {
    display: flex;
    align-items: baseline;
    gap: 5px;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.sug-aliases {
    color: #60a5fa;
    font-size: 0.72rem;
    overflow: hidden;
    text-overflow: ellipsis;
}

.sug-common {
    color: #9ca3af;
    font-size: 0.75rem;
    flex-shrink: 0;
    margin-left: 6px;
}

.toolbar-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
}

.sort-control {
    display: flex;
    align-items: center;
    gap: 6px;
}

.sort-control label {
    font-size: 0.75rem;
    color: #9ca3af;
    margin: 0;
}

.sort-select {
    background: #111827;
    color: #e5e7eb;
    border: 1px solid #4b5563;
    border-radius: 4px;
    padding: 3px 8px;
    font-size: 0.78rem;
    height: 26px;
    margin: 0;
}

.toolbar-right {
    display: flex;
    align-items: center;
    gap: 8px;
}

.list-stats {
    font-size: 0.75rem;
    color: #9ca3af;
    white-space: nowrap;
}

.list-stats strong {
    color: #10b981;
}

.toolbar-fav-btn {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    background: #111827;
    color: #9ca3af;
    border: 1px solid #4b5563;
    border-radius: 5px;
    padding: 2px 7px;
    font-size: 0.74rem;
    font-weight: 600;
    height: 26px;
    margin: 0;
    cursor: pointer;
    white-space: nowrap;
    transition: all 0.15s;
}

.toolbar-fav-btn:hover {
    border-color: #f59e0b;
    color: #fbbf24;
}

.toolbar-fav-btn.active {
    background: rgba(245, 158, 11, 0.18);
    border-color: rgba(245, 158, 11, 0.65);
    color: #fbbf24;
}

.card-fav-btn {
    background: transparent;
    border: none;
    color: #6b7280;
    font-size: 0.92rem;
    line-height: 1;
    padding: 0 2px 0 0;
    margin: 0;
    cursor: pointer;
    flex-shrink: 0;
    transition: color 0.15s, transform 0.12s;
}

.card-fav-btn:hover {
    color: #fbbf24;
    transform: scale(1.12);
}

.card-fav-btn.is-fav {
    color: #fbbf24;
    text-shadow: 0 0 6px rgba(245, 158, 11, 0.4);
}

.toolbar-dl-btn {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    background: rgba(16, 185, 129, 0.15);
    color: #34d399;
    border: 1px solid rgba(16, 185, 129, 0.45);
    border-radius: 5px;
    padding: 3px 8px;
    font-size: 0.74rem;
    font-weight: 600;
    height: 26px;
    margin: 0;
    cursor: pointer;
    white-space: nowrap;
    transition: background-color 0.15s, border-color 0.15s, color 0.15s;
}

.toolbar-dl-btn:hover:not(:disabled) {
    background: #10b981;
    color: #06281e;
    border-color: #10b981;
}

.toolbar-dl-btn:disabled {
    opacity: 0.45;
    cursor: not-allowed;
}

.toolbar-dl-btn.all-cached:disabled {
    opacity: 0.75;
    background: rgba(16, 185, 129, 0.08);
    border-color: rgba(16, 185, 129, 0.3);
    color: #10b981;
}

.toolbar-dl-btn.stop {
    background: rgba(239, 68, 68, 0.18);
    color: #f87171;
    border-color: rgba(239, 68, 68, 0.5);
}

.toolbar-dl-btn.stop:hover {
    background: #ef4444;
    color: #ffffff;
    border-color: #ef4444;
}

/* Scrollable List & Cards */
.scrollable-list {
    flex: 1;
    overflow-y: auto;
    padding: 6px;
}

.object-card {
    background: #111827;
    border: 1px solid #374151;
    margin-bottom: 6px;
    cursor: pointer;
    border-radius: 6px;
    overflow: hidden;
    transition: border-color 0.15s, background-color 0.15s;
}

.object-card:hover {
    background: #1f2937;
    border-color: #4b5563;
}

.object-card.active {
    border-color: #10b981;
    background: rgba(16, 185, 129, 0.08);
}

.object-card.search-match {
    border-left: 3px solid #60a5fa;
}

.card-content {
    display: flex;
    min-height: 76px;
    align-items: stretch;
}

.img-col {
    width: 68px;
    flex-shrink: 0;
    background: #000;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    position: relative;
}

.list-thumb {
    width: 100%;
    height: 100%;
    object-fit: cover;
}

.atlas-thumb-wrap {
    position: relative;
    width: 100%;
    height: 100%;
    display: flex;
}

.atlas-live-thumb {
    opacity: 0.94;
}

.atlas-thumb-dot {
    position: absolute;
    left: 4px;
    bottom: 4px;
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: #38bdf8;
    box-shadow: 0 0 4px rgba(56, 189, 248, 0.85), 0 0 0 1px rgba(15, 23, 42, 0.9);
    pointer-events: none;
}

.img-placeholder {
    width: 100%;
    height: 100%;
    background: #0f172a;
    color: #334155;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 1.2rem;
}

.info-col {
    flex: 1;
    padding: 6px 8px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    gap: 2px;
    min-width: 0;
}

.title-row {
    display: flex;
    align-items: center;
    gap: 5px;
    min-width: 0;
}

.info-col h4 {
    margin: 0;
    font-size: 0.88rem;
    line-height: 1.2;
    font-weight: 700;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    color: #f3f4f6;
}

.type-tag {
    font-size: 0.63rem;
    line-height: 1.2;
    background: #374151;
    color: #d1d5db;
    padding: 1px 5px;
    border-radius: 3px;
    white-space: nowrap;
    flex-shrink: 0;
}

.info-icon {
    background: transparent;
    border: none;
    cursor: pointer;
    color: #60a5fa;
    font-size: 0.82rem;
    padding: 0 2px;
    margin: 0;
    line-height: 1;
    flex-shrink: 0;
}

.status-indicator {
    margin-left: auto;
    font-size: 0.8rem;
    font-weight: bold;
    line-height: 1;
    white-space: nowrap;
    flex-shrink: 0;
}

.status-live { color: #38bdf8; font-size: 0.68rem; }
.status-cached { color: #10b981; }
.status-downloading { color: #f59e0b; }
.status-error { color: #ef4444; cursor: help; }

.spin-glyph {
    display: inline-block;
    animation: listSpin 1s linear infinite;
}

@keyframes listSpin {
    to { transform: rotate(360deg); }
}

.list-dl-strip {
    margin-top: 6px;
    padding: 5px 8px;
    border-radius: 5px;
    background: rgba(15, 23, 42, 0.85);
    border: 1px solid rgba(245, 158, 11, 0.35);
    display: flex;
    flex-direction: column;
    gap: 4px;
}

.list-dl-strip.summary {
    border-color: rgba(56, 189, 248, 0.4);
}

.list-dl-strip.summary.error {
    border-color: rgba(239, 68, 68, 0.55);
    background: rgba(127, 29, 29, 0.2);
}

.list-dl-strip-top {
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
}

.list-dl-spinner {
    width: 11px;
    height: 11px;
    border-radius: 50%;
    border: 2px solid rgba(251, 191, 36, 0.25);
    border-top-color: #fbbf24;
    animation: listSpin 0.8s linear infinite;
    flex-shrink: 0;
}

.list-dl-text {
    flex: 1;
    font-size: 0.7rem;
    line-height: 1.25;
    color: #e2e8f0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.list-dl-text strong {
    color: #fde68a;
    font-weight: 600;
}

.list-dl-sub {
    color: #94a3b8;
}

.list-dl-fail-pill {
    font-size: 0.66rem;
    font-weight: 700;
    color: #fca5a5;
    background: rgba(239, 68, 68, 0.2);
    border: 1px solid rgba(239, 68, 68, 0.45);
    border-radius: 4px;
    padding: 1px 5px;
    flex-shrink: 0;
    cursor: help;
}

.list-dl-dismiss {
    background: transparent;
    border: none;
    color: #94a3b8;
    font-size: 0.72rem;
    padding: 0 2px;
    cursor: pointer;
    flex-shrink: 0;
}

.list-dl-dismiss:hover {
    color: #f8fafc;
}

.list-dl-bar {
    height: 3px;
    border-radius: 999px;
    background: rgba(51, 65, 85, 0.8);
    overflow: hidden;
    position: relative;
}

.list-dl-bar-fill {
    height: 100%;
    background: linear-gradient(90deg, #10b981, #38bdf8);
    transition: width 0.25s ease;
}

.list-dl-bar-fill.indeterminate {
    width: 35%;
    animation: listBarSlide 1.2s ease-in-out infinite;
}

@keyframes listBarSlide {
    0% { transform: translateX(-100%); }
    100% { transform: translateX(300%); }
}

.common-name {
    font-size: 0.72rem;
    line-height: 1.25;
    color: #9ca3af;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.aliases-line {
    font-size: 0.66rem;
    line-height: 1.2;
    color: #60a5fa;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    opacity: 0.9;
}

.metrics-row {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 0.7rem;
    line-height: 1.2;
    color: #9ca3af;
    white-space: nowrap;
}

.metrics-row span {
    white-space: nowrap;
}

.metrics-row strong {
    color: #e5e7eb;
}

.vis-time {
    color: #10b981;
    font-weight: 600;
    margin-left: auto;
    white-space: nowrap;
}

.graph-col {
    width: 82px;
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 4px 5px;
    background: rgba(0, 0, 0, 0.35);
    border-left: 1px solid rgba(55, 65, 81, 0.5);
}

.altitude-chart {
    width: 100%;
    height: 38px;
}

.coord-label {
    font-size: 0.62rem;
    color: #6b7280;
    text-align: center;
    margin-top: 2px;
    white-space: nowrap;
}

.empty-list {
    text-align: center;
    color: #6b7280;
    padding: 30px 15px;
    font-size: 0.85rem;
}

/* Info Modal */
.modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.7);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 3000;
}

.modal-content {
    background: #1f2937;
    padding: 20px;
    border-radius: 8px;
    border: 1px solid #374151;
    max-width: 320px;
    width: 90%;
    color: #f3f4f6;
}

.modal-content h3 {
    margin-top: 0;
    border-bottom: 1px solid #374151;
    padding-bottom: 10px;
    margin-bottom: 10px;
}

.close-btn {
    margin-top: 15px;
    width: 100%;
    padding: 8px;
    background: #374151;
    border: none;
    color: white;
    border-radius: 4px;
    cursor: pointer;
}

.close-btn:hover {
    background: #4b5563;
}

@media (max-width: 768px) {
    .search-box {
        height: 38px;
    }

    .search-input {
        font-size: 16px !important;
    }

    .sort-select {
        height: 30px;
        font-size: 0.82rem;
    }

    .info-icon {
        padding: 4px 6px;
        font-size: 0.95rem;
    }
}
</style>
