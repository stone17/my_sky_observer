<script setup>
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue';
import SkyContextMap from './SkyContextMap.vue';
import ObservingDateConsole from './ObservingDateConsole.vue';
import {
    computeSensorFov,
    computeDefaultImageFov,
    normalizeImageFov,
    MIN_IMAGE_FOV_DEG
} from '../utils/fov.js';
import { parseObserverLocation } from '../utils/observingTime.js';
import {
    getObjectTypeLabel,
    getObjectTypeDescription,
    formatMagnitude
} from '../utils/catalogMeta.js';
import {
    ensureSkyAtlasLoaded,
    findOverlappingAtlasTiles,
    drawAtlasFrameToCanvas,
    skyAtlasVersion
} from '../utils/skyAtlas.js';
import { pendingSkyMapCenter, enterFrameModeToken } from '../utils/skyViewOptions.js';

const props = defineProps([
    'object',
    'objects',
    'filteredObjects',
    'settings',
    'clientSettings',
    'nightTimes',
    'searchQuery',
    'activeFov',
    'isLiveObsTime',
    'liveNowMs',
    'effectiveObsTimeMs'
]);
const emit = defineEmits([
    'update-settings',
    'update-client-settings',
    'update-search',
    'select-object',
    'frame-custom-position',
    'update-fov',
    'open-mobile-list',
    'update-object-settings',
    'update-framing-coords',
    'update-obs-time'
]);

const hasObserverLocation = computed(() => Boolean(parseObserverLocation(props.settings)));

// --- PHYSICAL SENSOR & DEFAULT IMAGE FOV CALCULATION (Single Source of Truth) ---
const systemSensorFov = computed(() => {
    return computeSensorFov(props.settings?.telescope, props.settings?.camera);
});

const defaultImageFov = computed(() => {
    return computeDefaultImageFov(
        props.settings?.telescope,
        props.settings?.camera,
        props.settings?.image_padding
    );
});

// --- LOCAL STATE ---
const localFov = ref(normalizeImageFov(props.activeFov, defaultImageFov.value));

watch(
    () => props.activeFov,
    (newVal) => {
        const normalized = normalizeImageFov(newVal, defaultImageFov.value);
        if (Math.abs(normalized - localFov.value) >= 0.01) {
            localFov.value = normalized;
        }
    },
    { immediate: true }
);

const isObjectDownloading = computed(() => {
    return props.object && props.object.status === 'downloading';
});

// --- INSTANT LOW-RES SKY ATLAS PREVIEW & HD CROSS-FADE ---
const hdImgRef = ref(null);
const loadedHdUrl = ref('');
const atlasPreviewCanvasRef = ref(null);

watch(
    () => props.object?.image_url,
    (newUrl) => {
        if (!newUrl) {
            loadedHdUrl.value = '';
            return;
        }
        if (newUrl !== loadedHdUrl.value) {
            nextTick(() => {
                const imgEl = hdImgRef.value;
                if (imgEl && imgEl.complete && imgEl.naturalWidth > 0 && props.object?.image_url === newUrl) {
                    loadedHdUrl.value = newUrl;
                }
            });
        }
    },
    { immediate: true }
);

const onHdImageLoad = () => {
    loadedHdUrl.value = props.object?.image_url || '';
};

const onHdImageError = () => {
    if (loadedHdUrl.value === props.object?.image_url) {
        loadedHdUrl.value = '';
    }
};

const isImageFovMatched = computed(() => {
    const imgFov = parseFloat(props.object?.image_fov);
    if (!Number.isFinite(imgFov) || imgFov <= 0) return false;
    return Math.abs(imgFov - localFov.value) < 0.02;
});

const previewFrameSpec = computed(() => {
    // Subscribe to atlas load / sheet decode updates
    void skyAtlasVersion.value;
    if (!props.object) return null;

    const ra = parseFloat(
        props.object.center_ra !== undefined ? props.object.center_ra : props.object.ra
    );
    const dec = parseFloat(
        props.object.center_dec !== undefined ? props.object.center_dec : props.object.dec
    );
    if (!Number.isFinite(ra) || !Number.isFinite(dec)) return null;

    const fov = parseFloat(
        localFov.value || props.object.image_fov || defaultImageFov.value
    );
    if (!Number.isFinite(fov) || fov <= 0.001) return null;

    const tiles = findOverlappingAtlasTiles(ra, dec, fov, props.object);
    return { ra, dec, fov, tiles };
});

const hasAtlasPreview = computed(() => {
    return Boolean(previewFrameSpec.value && previewFrameSpec.value.tiles.length > 0);
});

const isHdReady = computed(() => {
    return Boolean(
        props.object?.image_url &&
            loadedHdUrl.value === props.object.image_url &&
            !isObjectDownloading.value &&
            (isImageFovMatched.value || !hasAtlasPreview.value)
    );
});

const isFetchingSurvey = computed(() => {
    if (isObjectDownloading.value) return true;
    // Also true while browser is decoding a newly returned survey image at the current FOV
    return Boolean(
        props.object?.image_url &&
            loadedHdUrl.value !== props.object.image_url &&
            isImageFovMatched.value &&
            props.object?.status !== 'error'
    );
});

const effectiveDisplayFov = computed(() => {
    if (!isHdReady.value && hasAtlasPreview.value && previewFrameSpec.value) {
        return previewFrameSpec.value.fov;
    }
    const raw = parseFloat(props.object?.image_fov || localFov.value || defaultImageFov.value);
    return Number.isFinite(raw) && raw > 0.001 ? raw : 1.0;
});

const renderAtlasPreviewCanvas = () => {
    const canvas = atlasPreviewCanvasRef.value;
    const spec = previewFrameSpec.value;
    if (!canvas || !spec || !spec.tiles.length) return;

    drawAtlasFrameToCanvas(canvas, spec.ra, spec.dec, spec.fov, spec.tiles, {
        size: 512,
        requestLoad: true,
        highPriority: true,
        smoothingQuality: 'high'
    });
};

watch(
    [previewFrameSpec, isHdReady, skyAtlasVersion],
    () => {
        if (!isHdReady.value && hasAtlasPreview.value) {
            nextTick(renderAtlasPreviewCanvas);
        }
    },
    { immediate: true }
);

// --- FAVORITES BOOKMARKING ---
const isCurrentFavorite = computed(() => {
    if (!props.object?.name) return false;
    const favs = props.clientSettings?.favorites;
    return Array.isArray(favs) && favs.includes(props.object.name);
});

const toggleCurrentFavorite = () => {
    const name = props.object?.name;
    if (!name) return;
    const current = new Set(Array.isArray(props.clientSettings?.favorites) ? props.clientSettings.favorites : []);
    if (current.has(name)) {
        current.delete(name);
    } else {
        current.add(name);
    }
    emit('update-client-settings', {
        ...(props.clientSettings || {}),
        favorites: Array.from(current)
    });
};

// --- OBJECT OVERLAY TOGGLE & FOV PROJECTION ---
const showOverlays = computed(() => {
    return props.clientSettings?.show_object_overlays !== false;
});

const toggleOverlays = () => {
    const next = !showOverlays.value;
    emit('update-client-settings', {
        ...(props.clientSettings || {}),
        show_object_overlays: next
    });
};

// --- MULTI-PROFILE COMPARISON FOV OVERLAYS ---
const COMPARISON_COLORS = [
    { border: '#f59e0b', glow: 'rgba(245, 158, 11, 0.45)', bg: 'rgba(245, 158, 11, 0.2)' }, // Amber
    { border: '#06b6d4', glow: 'rgba(6, 182, 212, 0.45)', bg: 'rgba(6, 182, 212, 0.2)' },   // Cyan
    { border: '#a855f7', glow: 'rgba(168, 85, 247, 0.45)', bg: 'rgba(168, 85, 247, 0.2)' },  // Purple
    { border: '#84cc16', glow: 'rgba(132, 204, 22, 0.45)', bg: 'rgba(132, 204, 22, 0.2)' },  // Lime
    { border: '#ec4899', glow: 'rgba(236, 72, 153, 0.45)', bg: 'rgba(236, 72, 153, 0.2)' },  // Pink
    { border: '#3b82f6', glow: 'rgba(59, 130, 246, 0.45)', bg: 'rgba(59, 130, 246, 0.2)' }   // Blue
];

const showSetupsMenu = ref(false);
const setupsMenuRef = ref(null);

const selectedComparisonProfileNames = computed(() => {
    const list = props.clientSettings?.comparison_profiles;
    return Array.isArray(list) ? list : [];
});

const availableComparisonProfiles = computed(() => {
    const profilesObj = props.settings?.profiles || {};
    const activeName = props.settings?.active_profile || null;
    const enabledSet = new Set(selectedComparisonProfileNames.value);

    const names = Object.keys(profilesObj).sort((a, b) => a.localeCompare(b));
    const list = [];

    names.forEach((name, idx) => {
        if (name === activeName) return;
        const prof = profilesObj[name];
        if (!prof) return;
        const sFov = computeSensorFov(prof.telescope, prof.camera);
        if (sFov.w <= 0 || sFov.h <= 0) return;

        const color = COMPARISON_COLORS[idx % COMPARISON_COLORS.length];
        list.push({
            name,
            sensorFov: sFov,
            focalLength: prof.telescope?.focal_length,
            color,
            enabled: enabledSet.has(name)
        });
    });

    return list;
});

const toggleComparisonProfile = (name) => {
    const current = new Set(selectedComparisonProfileNames.value);
    if (current.has(name)) {
        current.delete(name);
    } else {
        current.add(name);
    }
    emit('update-client-settings', {
        ...(props.clientSettings || {}),
        comparison_profiles: Array.from(current)
    });
};

const setAllComparisonProfiles = (enableAll) => {
    const next = enableAll ? availableComparisonProfiles.value.map((p) => p.name) : [];
    emit('update-client-settings', {
        ...(props.clientSettings || {}),
        comparison_profiles: next
    });
};

const handleDocumentClick = (e) => {
    if (showSetupsMenu.value && setupsMenuRef.value && !setupsMenuRef.value.contains(e.target)) {
        showSetupsMenu.value = false;
    }
};

const parseSizeArcmin = (obj) => {
    if (!obj) return 0;
    if (typeof obj.maj_ax === 'number' && !Number.isNaN(obj.maj_ax) && obj.maj_ax > 0) {
        return obj.maj_ax;
    }
    if (typeof obj.size === 'string') {
        const m = obj.size.match(/([0-9]+(?:\.[0-9]+)?)/);
        if (m) return parseFloat(m[1]);
    }
    return 0;
};

const fovOverlayObjects = computed(() => {
    if (!showOverlays.value || !props.object) return [];

    const imgFov = effectiveDisplayFov.value;
    if (!imgFov || imgFov <= 0.001) return [];

    const centerRa = parseFloat(
        props.object.center_ra !== undefined ? props.object.center_ra : props.object.ra
    );
    const centerDec = parseFloat(
        props.object.center_dec !== undefined ? props.object.center_dec : props.object.dec
    );
    if (Number.isNaN(centerRa) || Number.isNaN(centerDec)) return [];

    const cosDec = Math.max(Math.cos((centerDec * Math.PI) / 180.0), 0.01);
    const halfFov = imgFov * 0.5;

    const projectCoord = (ra, dec) => {
        const numRa = parseFloat(ra);
        const numDec = parseFloat(dec);
        if (Number.isNaN(numRa) || Number.isNaN(numDec)) return null;

        let dRa = numRa - centerRa;
        if (dRa > 180) dRa -= 360;
        if (dRa < -180) dRa += 360;
        const dDec = numDec - centerDec;

        // Standard astronomical sky survey orientation: North is up (-Y), East is left (-X)
        const dxDeg = -dRa * cosDec;
        const dyDeg = -dDec;

        if (Math.abs(dxDeg) > halfFov * 1.02 || Math.abs(dyDeg) > halfFov * 1.02) {
            return null;
        }

        const xPct = 50 + (dxDeg / imgFov) * 100;
        const yPct = 50 + (dyDeg / imgFov) * 100;
        return { xPct, yPct, dxDeg, dyDeg };
    };

    const computeDiameterPct = (majArcmin, isPrimary) => {
        const sizeDeg = (majArcmin || 0) / 60.0;
        const rawPct = (sizeDeg / imgFov) * 100;
        const minPct = isPrimary ? 4.5 : 3.2;
        return Math.min(Math.max(rawPct, minPct), 92.0);
    };

    const items = [];

    // 1. Primary selected object (skip synthetic custom sky positions)
    if (!props.object.is_custom) {
        const primaryProj = projectCoord(props.object.ra, props.object.dec);
        const primaryMaj = parseSizeArcmin(props.object);
        if (primaryProj) {
            items.push({
                key: `primary-${props.object.name}`,
                name: props.object.name,
                commonName:
                    props.object.common_name && props.object.common_name !== 'N/A'
                        ? props.object.common_name
                        : '',
                aliases: props.object.aliases || '',
                type: props.object.type || '',
                ra: parseFloat(props.object.ra),
                dec: parseFloat(props.object.dec),
                majArcmin: primaryMaj,
                xPct: primaryProj.xPct,
                yPct: primaryProj.yPct,
                diameterPct: computeDiameterPct(primaryMaj, true),
                isPrimary: true,
                isSubObject: false,
                catalogRef: props.object
            });
        }
    }

    // Helper to check if a candidate is too close (< 0.35 arcmin) to an existing marker
    const isNearExisting = (ra, dec) => {
        const numRa = parseFloat(ra);
        const numDec = parseFloat(dec);
        for (const ex of items) {
            let dRa = (numRa - ex.ra) * cosDec;
            let dDec = numDec - ex.dec;
            const distArcmin = Math.hypot(dRa, dDec) * 60.0;
            if (distArcmin < 0.35) {
                return ex;
            }
        }
        return null;
    };

    // 2. Collated sub-objects belonging to the primary object
    if (Array.isArray(props.object.sub_objects)) {
        for (const sub of props.object.sub_objects) {
            const proj = projectCoord(sub.ra, sub.dec);
            if (!proj) continue;
            const near = isNearExisting(sub.ra, sub.dec);
            if (near) continue;

            const subMaj = parseSizeArcmin(sub);
            items.push({
                key: `sub-${sub.name}`,
                name: sub.name,
                commonName:
                    sub.common_name && sub.common_name !== 'N/A' ? sub.common_name : '',
                aliases: '',
                type: sub.type || '',
                ra: parseFloat(sub.ra),
                dec: parseFloat(sub.dec),
                majArcmin: subMaj,
                xPct: proj.xPct,
                yPct: proj.yPct,
                diameterPct: computeDiameterPct(subMaj, false),
                isPrimary: false,
                isSubObject: true,
                catalogRef: null
            });
        }
    }

    // 3. Other catalog objects (and their sub-objects) falling within the current FOV
    const allObjs = Array.isArray(props.objects) ? props.objects : [];
    const secondaryCandidates = [];

    for (const cand of allObjs) {
        if (!cand || cand.name === props.object.name) continue;
        // Quick declination bounding box reject before full projection
        if (Math.abs(parseFloat(cand.dec) - centerDec) > halfFov * 1.1) continue;

        const proj = projectCoord(cand.ra, cand.dec);
        if (proj) {
            secondaryCandidates.push({
                cand,
                sub: null,
                proj,
                maj: parseSizeArcmin(cand),
                mag: typeof cand.mag === 'number' ? cand.mag : 99
            });
        }

        if (Array.isArray(cand.sub_objects)) {
            for (const sub of cand.sub_objects) {
                if (Math.abs(parseFloat(sub.dec) - centerDec) > halfFov * 1.1) continue;
                const subProj = projectCoord(sub.ra, sub.dec);
                if (subProj) {
                    secondaryCandidates.push({
                        cand,
                        sub,
                        proj: subProj,
                        maj: parseSizeArcmin(sub),
                        mag: typeof sub.mag === 'number' ? sub.mag : 99
                    });
                }
            }
        }
    }

    // Sort secondary candidates by prominence (larger size first, then brighter magnitude)
    secondaryCandidates.sort((a, b) => {
        if (Math.abs(b.maj - a.maj) > 1.0) return b.maj - a.maj;
        return a.mag - b.mag;
    });

    for (const entry of secondaryCandidates) {
        if (items.length >= 25) break;
        const target = entry.sub || entry.cand;
        const near = isNearExisting(target.ra, target.dec);
        if (near) continue;

        items.push({
            key: entry.sub ? `fov-sub-${target.name}` : `fov-${target.name}`,
            name: target.name,
            commonName:
                target.common_name && target.common_name !== 'N/A'
                    ? target.common_name
                    : '',
            aliases: target.aliases || '',
            type: target.type || '',
            ra: parseFloat(target.ra),
            dec: parseFloat(target.dec),
            majArcmin: entry.maj,
            xPct: entry.proj.xPct,
            yPct: entry.proj.yPct,
            diameterPct: computeDiameterPct(entry.maj, false),
            isPrimary: false,
            isSubObject: Boolean(entry.sub),
            catalogRef: entry.cand
        });
    }

    return items;
});

const onOverlayObjectClick = (marker) => {
    if (marker.catalogRef && marker.catalogRef.name !== props.object?.name) {
        emit('select-object', marker.catalogRef);
    }
};

// --- SENSOR RECTANGLE STYLE ---
const sensorStyle = computed(() => {
    const sFov = systemSensorFov.value;
    const baseFov = localFov.value || props.object?.image_fov || defaultImageFov.value;

    if (sFov.w <= 0 || baseFov <= 0.001) return { display: 'none' };

    const wPct = (sFov.w / baseFov) * 100;
    const hPct = (sFov.h / baseFov) * 100;

    return {
        width: `${wPct}%`,
        height: `${hPct}%`,
        position: 'absolute',
        top: `calc(50% + ${offsetY.value}px)`,
        left: `calc(50% + ${offsetX.value}px)`,
        transform: `translate(-50%, -50%) rotate(${rotation.value}deg)`,
        border: '2px solid #ef4444',
        boxShadow: '0 0 12px rgba(239, 68, 68, 0.5)',
        cursor: 'move',
        touchAction: 'none',
        zIndex: 15
    };
});

const activeComparisonRectangles = computed(() => {
    const baseFov = localFov.value || props.object?.image_fov || defaultImageFov.value;
    if (baseFov <= 0.001) return [];

    return availableComparisonProfiles.value
        .filter((p) => p.enabled)
        .map((p) => {
            const wPct = (p.sensorFov.w / baseFov) * 100;
            const hPct = (p.sensorFov.h / baseFov) * 100;
            return {
                ...p,
                style: {
                    width: `${wPct}%`,
                    height: `${hPct}%`,
                    position: 'absolute',
                    top: `calc(50% + ${offsetY.value}px)`,
                    left: `calc(50% + ${offsetX.value}px)`,
                    transform: `translate(-50%, -50%) rotate(${rotation.value}deg)`,
                    border: `2px dashed ${p.color.border}`,
                    boxShadow: `0 0 10px ${p.color.glow}`,
                    pointerEvents: 'none',
                    zIndex: 14
                }
            };
        });
});

// --- ROTATION & DRAG LOGIC ---
const rotation = ref(0);
let rotInterval = null;
let rotTimeout = null;
let isRestoringObjectState = false;

const emitObjectPatch = (patch) => {
    if (!props.object?.name || props.object?.is_custom || isRestoringObjectState) return;
    emit('update-object-settings', {
        name: props.object.name,
        patch
    });
};

const startRotate = (dir) => {
    stopRotate();
    const update = () => {
        rotation.value = (rotation.value + dir + 360) % 360;
    };
    update();
    rotTimeout = setTimeout(() => {
        rotInterval = setInterval(update, 30);
    }, 300);
};

const stopRotate = () => {
    if (rotTimeout) clearTimeout(rotTimeout);
    if (rotInterval) clearInterval(rotInterval);
    rotTimeout = null;
    rotInterval = null;
};

watch(rotation, (newRot) => {
    if (!props.object || isRestoringObjectState) return;
    const cleanRot = Number.isFinite(Number(newRot)) ? Number(newRot) : 0;
    props.object.rotation = cleanRot;
    emitObjectPatch({ rotation: cleanRot });
});

const offsetX = ref(0);
const offsetY = ref(0);
const skyMapSensorOverride = ref(null);
const isDragging = ref(false);
const dragStart = ref({ x: 0, y: 0 });
const pinchStartDist = ref(0);
const pinchStartZoom = ref(1.0);

const viewportRef = ref(null);
const wrapperSize = ref(0);
let resizeObserver = null;

const zoomLevel = ref(1.0);

const applySavedObjectState = (objName) => {
    if (!objName) return;
    isRestoringObjectState = true;
    skyMapSensorOverride.value = null;
    const saved = props.clientSettings?.object_settings?.[objName] || {};
    const savedRot = saved.rotation !== undefined ? Number(saved.rotation) : Number(props.object?.rotation || 0);
    rotation.value = Number.isFinite(savedRot) ? savedRot : 0;
    if (wrapperSize.value > 0 && (saved.offset_x_pct || saved.offset_y_pct)) {
        offsetX.value = ((saved.offset_x_pct || 0) / 100) * wrapperSize.value;
        offsetY.value = ((saved.offset_y_pct || 0) / 100) * wrapperSize.value;
    } else {
        offsetX.value = 0;
        offsetY.value = 0;
    }
    zoomLevel.value = 1.0;
    isRestoringObjectState = false;
};

watch(() => props.object, (newObj, oldObj) => {
    if (newObj && (newObj.name !== oldObj?.name || (newObj.is_custom && newObj !== oldObj))) {
        applySavedObjectState(newObj.name);
    }
}, { immediate: true });

const stepFov = (multiplier) => {
    const next = Math.max(localFov.value * multiplier, MIN_IMAGE_FOV_DEG);
    localFov.value = parseFloat(next.toFixed(2));
    emit('update-fov', { fov: localFov.value, skipDownload: true });
};

const onFovInputChange = () => {
    localFov.value = normalizeImageFov(localFov.value, defaultImageFov.value);
    emit('update-fov', { fov: localFov.value, skipDownload: true });
};

const resetFov = () => {
    skyMapSensorOverride.value = null;
    offsetX.value = 0;
    offsetY.value = 0;
    zoomLevel.value = 1.0;
    localFov.value = defaultImageFov.value;
    emit('update-fov', { fov: defaultImageFov.value, isReset: true });
};

const onSkyMapUpdateSensorCenter = (coords) => {
    if (!coords || !props.object) return;
    const ra = ((Number(coords.ra) % 360) + 360) % 360;
    const dec = Math.max(-89.99, Math.min(89.99, Number(coords.dec)));
    if (!Number.isFinite(ra) || !Number.isFinite(dec)) return;

    const centerRa = parseFloat(
        props.object.center_ra !== undefined ? props.object.center_ra : props.object.ra
    );
    const centerDec = parseFloat(
        props.object.center_dec !== undefined ? props.object.center_dec : props.object.dec
    );
    let dRa = ra - centerRa;
    if (dRa > 180) dRa -= 360;
    if (dRa < -180) dRa += 360;
    const dDec = dec - centerDec;

    const baseFov = effectiveDisplayFov.value || localFov.value || 1.0;
    const cosDec = Math.max(Math.cos((centerDec * Math.PI) / 180), 0.01);
    const angOffsetX = dRa * cosDec;
    const angOffsetY = dDec;

    if (
        wrapperSize.value > 0 &&
        Math.abs(angOffsetX) <= baseFov * 0.48 &&
        Math.abs(angOffsetY) <= baseFov * 0.48
    ) {
        const degToPx = wrapperSize.value / baseFov;
        offsetX.value = -angOffsetX * degToPx;
        offsetY.value = -angOffsetY * degToPx;
        skyMapSensorOverride.value = null;
        if (coords.persist && props.object.name) {
            emitObjectPatch({
                offset_x_pct: Math.round((offsetX.value / wrapperSize.value) * 10000) / 100,
                offset_y_pct: Math.round((offsetY.value / wrapperSize.value) * 10000) / 100
            });
        }
    } else {
        skyMapSensorOverride.value = { ra, dec };
    }
};

const onSkyMapResetSensorCenter = () => {
    skyMapSensorOverride.value = null;
    offsetX.value = 0;
    offsetY.value = 0;
    if (props.object?.name) {
        emitObjectPatch({
            offset_x_pct: 0,
            offset_y_pct: 0
        });
    }
};

const showSkyMap = ref(false);
const skyMapInitialFov = ref(35.0);

const currentBaseImageFov = computed(() => {
    const val = parseFloat(props.object?.image_fov || localFov.value || defaultImageFov.value);
    return Number.isFinite(val) && val > 0 ? val : 1.0;
});

let skyMapEntryOffset = null;

const openSkyMap = (targetFov = null) => {
    if (props.object?.name && !props.object?.is_custom) {
        const saved = props.clientSettings?.object_settings?.[props.object.name] || {};
        skyMapEntryOffset = {
            name: props.object.name,
            offset_x_pct: Number(saved.offset_x_pct) || 0,
            offset_y_pct: Number(saved.offset_y_pct) || 0
        };
    } else {
        skyMapEntryOffset = null;
    }
    const base = currentBaseImageFov.value;
    skyMapInitialFov.value =
        targetFov !== null && Number.isFinite(targetFov)
            ? Math.max(targetFov, base * 1.12)
            : Math.max(35.0, base * 4.0);
    showSkyMap.value = true;
};

const closeSkyMap = () => {
    skyMapEntryOffset = null;
    showSkyMap.value = false;
    zoomLevel.value = 1.0;
};

watch(pendingSkyMapCenter, (req) => {
    if (req && !showSkyMap.value && props.object) {
        openSkyMap(Number.isFinite(req.fov) ? req.fov : 30.0);
    }
});

watch(enterFrameModeToken, () => {
    if (showSkyMap.value) {
        closeSkyMap();
    }
});

const onSkyMapFramePosition = (payload) => {
    if (!payload) return;
    const ra = Number(payload.ra);
    const dec = Number(payload.dec);
    if (!Number.isFinite(ra) || !Number.isFinite(dec)) return;

    if (
        skyMapEntryOffset &&
        props.object?.name === skyMapEntryOffset.name &&
        !props.object?.is_custom
    ) {
        emitObjectPatch({
            offset_x_pct: skyMapEntryOffset.offset_x_pct,
            offset_y_pct: skyMapEntryOffset.offset_y_pct
        });
    }
    skyMapEntryOffset = null;

    skyMapSensorOverride.value = null;
    offsetX.value = 0;
    offsetY.value = 0;
    zoomLevel.value = 1.0;
    showSkyMap.value = false;

    emit('frame-custom-position', {
        ra,
        dec,
        constellation: payload.constellation || '',
        fov: localFov.value,
        rotation: rotation.value,
        cachedImages: payload.cachedImages || null
    });

    nextTick(() => {
        if (props.object?.name && !props.object?.is_custom) {
            applySavedObjectState(props.object.name);
        }
    });
};

const onWheel = (e) => {
    if (showSkyMap.value) return;
    if (e.shiftKey) {
        // Shift + Wheel rotates sensor
        const dir = e.deltaY < 0 ? 2 : -2;
        rotation.value = (rotation.value + dir + 360) % 360;
        return;
    }
    if (e.deltaY < 0) {
        zoomLevel.value = Math.min(zoomLevel.value * 1.1, 10.0);
    } else if (e.deltaY > 0) {
        if (zoomLevel.value <= 1.005) {
            openSkyMap(currentBaseImageFov.value * 1.18);
            return;
        }
        zoomLevel.value = Math.max(zoomLevel.value / 1.1, 1.0);
    }
};

const resetZoom = () => {
    zoomLevel.value = 1.0;
};

const onDrag = (e) => {
    if (isDragging.value) {
        offsetX.value = (e.clientX - dragStart.value.x) / zoomLevel.value;
        offsetY.value = (e.clientY - dragStart.value.y) / zoomLevel.value;
    }
};
const startDrag = (e) => {
    skyMapSensorOverride.value = null;
    isDragging.value = true;
    dragStart.value = {
        x: e.clientX - (offsetX.value * zoomLevel.value),
        y: e.clientY - (offsetY.value * zoomLevel.value)
    };
    e.preventDefault();
};
const stopDrag = () => {
    const wasDragging = isDragging.value;
    isDragging.value = false;
    pinchStartDist.value = 0;
    if (wasDragging && props.object?.name && wrapperSize.value > 0) {
        emitObjectPatch({
            offset_x_pct: Math.round((offsetX.value / wrapperSize.value) * 10000) / 100,
            offset_y_pct: Math.round((offsetY.value / wrapperSize.value) * 10000) / 100
        });
    }
};

const getTouchDistance = (t1, t2) => {
    return Math.hypot(t2.clientX - t1.clientX, t2.clientY - t1.clientY);
};

const startTouchDrag = (e) => {
    if (e.touches.length === 1) {
        skyMapSensorOverride.value = null;
        const t = e.touches[0];
        isDragging.value = true;
        dragStart.value = {
            x: t.clientX - (offsetX.value * zoomLevel.value),
            y: t.clientY - (offsetY.value * zoomLevel.value)
        };
    }
};

const onViewportTouchStart = (e) => {
    if (e.touches.length === 2) {
        isDragging.value = false;
        pinchStartDist.value = getTouchDistance(e.touches[0], e.touches[1]);
        pinchStartZoom.value = zoomLevel.value;
    }
};

const onViewportTouchMove = (e) => {
    if (showSkyMap.value) return;
    if (e.touches.length === 2 && pinchStartDist.value > 0) {
        const dist = getTouchDistance(e.touches[0], e.touches[1]);
        const scale = dist / pinchStartDist.value;
        const candidateZoom = pinchStartZoom.value * scale;
        if (candidateZoom < 0.86) {
            pinchStartDist.value = 0;
            openSkyMap(currentBaseImageFov.value * 1.25);
            e.preventDefault();
            return;
        }
        zoomLevel.value = Math.min(Math.max(candidateZoom, 1.0), 10.0);
        e.preventDefault();
        return;
    }
    if (isDragging.value && e.touches.length === 1) {
        const t = e.touches[0];
        offsetX.value = (t.clientX - dragStart.value.x) / zoomLevel.value;
        offsetY.value = (t.clientY - dragStart.value.y) / zoomLevel.value;
        e.preventDefault();
    }
};

const getCurrentCenterCoordinates = () => {
    if (skyMapSensorOverride.value) {
        return {
            ra: skyMapSensorOverride.value.ra,
            dec: skyMapSensorOverride.value.dec
        };
    }

    const baseFov = effectiveDisplayFov.value || localFov.value;
    const pxToDeg = baseFov / (wrapperSize.value || 1);

    const centerRa = parseFloat(props.object?.center_ra !== undefined ? props.object.center_ra : props.object.ra);
    const centerDec = parseFloat(props.object?.center_dec !== undefined ? props.object.center_dec : props.object.dec);

    const decOffset = -(offsetY.value * pxToDeg);
    const cosDec = Math.max(Math.cos(centerDec * Math.PI / 180), 0.01);
    const raOffset = -(offsetX.value * pxToDeg) / cosDec;

    return {
        ra: centerRa + raOffset,
        dec: centerDec + decOffset
    };
};

const currentSensorCenter = computed(() => {
    if (!props.object) return null;
    return getCurrentCenterCoordinates();
});

let lastEmittedFramingCoords = null;
watch(
    [currentSensorCenter, () => props.object?.name, () => props.object?.ra, () => props.object?.dec],
    ([ctr]) => {
        if (!props.object || !ctr || !Number.isFinite(ctr.ra) || !Number.isFinite(ctr.dec)) {
            if (lastEmittedFramingCoords !== null) {
                lastEmittedFramingCoords = null;
                emit('update-framing-coords', null);
            }
            return;
        }

        const catRa = parseFloat(props.object.ra);
        const catDec = parseFloat(props.object.dec);
        const normRa = (((ctr.ra % 360) + 360) % 360);
        const clampedDec = Math.max(-89.99, Math.min(89.99, ctr.dec));

        let isDetached = Boolean(skyMapSensorOverride.value);
        if (!isDetached && Number.isFinite(catRa) && Number.isFinite(catDec)) {
            let dRa = normRa - (((catRa % 360) + 360) % 360);
            if (dRa > 180) dRa -= 360;
            if (dRa < -180) dRa += 360;
            const dDec = clampedDec - catDec;
            const cosD = Math.cos(((catDec + clampedDec) * 0.5 * Math.PI) / 180.0);
            const sepDeg = Math.hypot(dRa * cosD, dDec);
            isDetached = sepDeg > 0.02;
        }

        if (!isDetached) {
            if (lastEmittedFramingCoords !== null) {
                lastEmittedFramingCoords = null;
                emit('update-framing-coords', null);
            }
            return;
        }

        if (
            !lastEmittedFramingCoords ||
            Math.abs(normRa - lastEmittedFramingCoords.ra) > 1e-4 ||
            Math.abs(clampedDec - lastEmittedFramingCoords.dec) > 1e-4
        ) {
            lastEmittedFramingCoords = { ra: normRa, dec: clampedDec };
            emit('update-framing-coords', lastEmittedFramingCoords);
        }
    },
    { immediate: true }
);

const isZoomModified = computed(() => Math.abs(zoomLevel.value - 1.0) > 0.01);

const isFovModified = computed(() => {
    if (!props.object) return false;
    const currentImgFov = props.object.image_fov || defaultImageFov.value;
    return Math.abs(localFov.value - currentImgFov) >= 0.01;
});

const isCustomFov = computed(() => {
    const currentImgFov = props.object?.image_fov || localFov.value;
    return (
        Boolean(skyMapSensorOverride.value) ||
        Math.abs(localFov.value - defaultImageFov.value) >= 0.01 ||
        Math.abs(currentImgFov - defaultImageFov.value) >= 0.01 ||
        Boolean(props.object?.custom_framed)
    );
});

const isModified = computed(() => {
    if (!props.object) return false;
    if (skyMapSensorOverride.value) return true;
    if (Math.abs(offsetX.value) > 0.01 || Math.abs(offsetY.value) > 0.01) return true;
    return isFovModified.value;
});

// --- HIGH-RESOLUTION IMAGE DOWNLOAD SUPPORT ---
const baseResolution = computed(() => {
    return Number(props.settings?.image_server?.resolution) || 1024;
});

const highResTarget = computed(() => {
    const base = baseResolution.value;
    if (base < 2048) return 2048;
    if (base < 3072) return 3072;
    return 4096;
});

const currentImageResolution = computed(() => {
    const explicit = Number(props.object?.image_resolution);
    if (Number.isFinite(explicit) && explicit >= 64) return explicit;
    const url = String(props.object?.image_url || '');
    const match = url.match(/(?:_r|_res)(\d{3,4})(?:_|\/)/);
    if (match) {
        const parsed = Number(match[1]);
        if (Number.isFinite(parsed) && parsed >= 64) return parsed;
    }
    return baseResolution.value;
});

const isHighResLoaded = computed(() => {
    return Boolean(
        isHdReady.value &&
            !isModified.value &&
            currentImageResolution.value >= highResTarget.value
    );
});

const activeFetchingResolution = computed(() => {
    const dlRes = Number(props.object?.downloading_resolution);
    if (Number.isFinite(dlRes) && dlRes >= 64) return dlRes;
    return baseResolution.value;
});

const isFetchingHighRes = computed(() => {
    return isFetchingSurvey.value && activeFetchingResolution.value >= highResTarget.value;
});

const updateImage = async (explicitRes = null) => {
    if (!props.object) return;

    const requestedRes =
        typeof explicitRes === 'number' && Number.isFinite(explicitRes) && explicitRes >= 64
            ? Math.min(4096, Math.round(explicitRes))
            : null;

    const targetFov = normalizeImageFov(localFov.value, defaultImageFov.value);
    localFov.value = targetFov;

    const hasOffset =
        Boolean(skyMapSensorOverride.value) ||
        Math.abs(offsetX.value) > 0.01 ||
        Math.abs(offsetY.value) > 0.01;
    const isOffCenter =
        hasOffset || Boolean(props.object.custom_framed) || Boolean(props.object.is_custom);

    // If centered on catalog coordinates, use standard object download via App.vue
    if (!isOffCenter) {
        emit('update-fov', { fov: targetFov, resolution: requestedRes });
        return;
    }

    const coords = getCurrentCenterCoordinates();
    const effectiveReqRes = requestedRes || baseResolution.value;

    props.object.status = 'downloading';
    props.object.downloading_resolution = effectiveReqRes;
    emit('update-fov', { fov: targetFov, skipDownload: true });

    try {
        const res = await fetch('/api/fetch-custom-image', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ra: coords.ra,
                dec: coords.dec,
                fov: targetFov,
                resolution: effectiveReqRes,
                source: props.settings.image_server?.source || "dss2r",
                timeout: props.settings.image_server?.timeout || 120
            })
        });

        if (res.ok) {
            const data = await res.json();
            const uniqueUrl = `${data.url}?t=${new Date().getTime()}`;
            const savedRes = Number(data.resolution) || effectiveReqRes;

            props.object.image_url = uniqueUrl;
            props.object.center_ra = coords.ra;
            props.object.center_dec = coords.dec;
            props.object.custom_framed = true;
            props.object.image_fov = data.image_fov || targetFov;
            props.object.image_resolution = savedRes;
            delete props.object.downloading_resolution;
            props.object.status = 'cached';

            skyMapSensorOverride.value = null;
            offsetX.value = 0;
            offsetY.value = 0;
            zoomLevel.value = 1.0;

            emitObjectPatch({
                fov: props.object.image_fov,
                is_manual_fov: true,
                custom_framed: true,
                center_ra: coords.ra,
                center_dec: coords.dec,
                image_url: data.url,
                image_resolution: savedRes,
                offset_x_pct: 0,
                offset_y_pct: 0,
                rotation: rotation.value
            });
        } else {
            delete props.object.downloading_resolution;
            props.object.status = 'error';
        }
    } catch (e) {
        console.error(e);
        delete props.object.downloading_resolution;
        props.object.status = 'error';
    }
};

const downloadHighResImage = () => {
    updateImage(highResTarget.value);
};

const ninaStatus = ref("");
const sendToNina = async () => {
    const coords = getCurrentCenterCoordinates();
    ninaStatus.value = "Sending...";
    try {
        const res = await fetch('/api/nina/framing', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ra: coords.ra,
                dec: coords.dec,
                rotation: rotation.value || 0.0
            })
        });
        if (res.ok) {
            ninaStatus.value = "Sent!";
        } else {
            ninaStatus.value = "Failed!";
        }
    } catch (e) {
        ninaStatus.value = "Error!";
    }
    setTimeout(() => { ninaStatus.value = ""; }, 3000);
};

onMounted(() => {
    ensureSkyAtlasLoaded();
    if (viewportRef.value) {
        resizeObserver = new ResizeObserver(entries => {
            for (let entry of entries) {
                const prevSize = wrapperSize.value;
                wrapperSize.value = Math.min(entry.contentRect.width, entry.contentRect.height);
                if (prevSize === 0 && wrapperSize.value > 0 && props.object?.name) {
                    const saved = props.clientSettings?.object_settings?.[props.object.name];
                    if (saved && (saved.offset_x_pct || saved.offset_y_pct)) {
                        offsetX.value = ((saved.offset_x_pct || 0) / 100) * wrapperSize.value;
                        offsetY.value = ((saved.offset_y_pct || 0) / 100) * wrapperSize.value;
                    }
                }
            }
        });
        resizeObserver.observe(viewportRef.value);
    }
    document.addEventListener('click', handleDocumentClick);
});
onUnmounted(() => {
    if (resizeObserver) resizeObserver.disconnect();
    document.removeEventListener('click', handleDocumentClick);
    stopRotate();
    emit('update-framing-coords', null);
});
</script>

<template>
    <article class="framing-panel">
        <!-- Streamlined Object Info & Primary Action Header -->
        <header class="framing-header">
            <div class="target-identity">
                <div class="title-line">
                    <button
                        v-if="!object.is_custom"
                        type="button"
                        class="fav-star-btn"
                        :class="{ 'is-fav': isCurrentFavorite }"
                        @click="toggleCurrentFavorite"
                        :title="isCurrentFavorite ? 'Remove from Favorites' : 'Save target to Favorites'"
                        :aria-label="isCurrentFavorite ? 'Remove from Favorites' : 'Save target to Favorites'"
                    >
                        {{ isCurrentFavorite ? '★' : '☆' }}
                    </button>
                    <h2>{{ object.name }}</h2>
                    <span
                        v-if="object.type"
                        class="meta-badge accent"
                        :title="`${getObjectTypeLabel(object.type)} (${object.type}) — ${getObjectTypeDescription(object.type)}`"
                    >
                        {{ getObjectTypeLabel(object.type) }}
                    </span>
                    <span v-if="object.constellation" class="meta-badge">{{ object.constellation }}</span>
                    <span
                        v-if="object.aliases"
                        class="aliases-badge"
                        :title="`Same-names / collated IDs: ${object.aliases}`"
                    >
                        ≡ {{ object.aliases }}
                    </span>
                </div>
                <div class="subtitle-line">
                    <span v-if="object.common_name && object.common_name !== 'N/A'" class="common-title">
                        {{ object.common_name }}
                    </span>
                    <span class="meta-sep" v-if="object.common_name && object.common_name !== 'N/A'">•</span>
                    <template v-if="!object.is_custom">
                        <span>Mag: <strong>{{ formatMagnitude(object.mag) }}</strong></span>
                        <span class="meta-sep">•</span>
                        <span>Size: <strong>{{ object.size }}</strong></span>
                        <span class="meta-sep">•</span>
                    </template>
                    <span>RA/DEC: <strong>{{ typeof object.ra === 'number' ? object.ra.toFixed(2) : object.ra }}° / {{ typeof object.dec === 'number' ? object.dec.toFixed(2) : object.dec }}°</strong></span>
                </div>
            </div>

            <div class="header-actions">
                <button
                    type="button"
                    class="nina-btn"
                    @click="sendToNina"
                    title="Send current framing coordinates and rotation to N.I.N.A. (Requires Advanced API plugin)"
                >
                    <span>📡</span>
                    <span class="nina-btn-text">{{ ninaStatus || 'Send to N.I.N.A.' }}</span>
                    <span class="nina-btn-short">{{ ninaStatus || 'N.I.N.A.' }}</span>
                </button>
            </div>
        </header>

        <!-- Main Sky Viewport -->
        <div
            class="framing-viewport"
            ref="viewportRef"
            @mousemove="onDrag"
            @mouseup="stopDrag"
            @mouseleave="stopDrag"
            @touchstart="onViewportTouchStart"
            @touchmove="onViewportTouchMove"
            @touchend="stopDrag"
            @touchcancel="stopDrag"
            @wheel.prevent="onWheel"
        >
            <!-- Wide-Field Interactive Sky Context Map (on zoom-out or Sky Map button) -->
            <SkyContextMap
                v-if="showSkyMap && object"
                :object="object"
                :objects="objects"
                :filteredObjects="filteredObjects"
                :settings="settings"
                :clientSettings="clientSettings"
                :nightTimes="nightTimes"
                :sensorFov="systemSensorFov"
                :comparisonRectangles="activeComparisonRectangles"
                :sensorCenter="currentSensorCenter"
                :rotation="rotation"
                :baseImageFov="currentBaseImageFov"
                :initialMapFov="skyMapInitialFov"
                :isLiveObsTime="isLiveObsTime"
                :liveNowMs="liveNowMs"
                :effectiveObsTimeMs="effectiveObsTimeMs"
                @close="closeSkyMap"
                @select-object="emit('select-object', $event)"
                @frame-position="onSkyMapFramePosition"
                @update-sensor-center="onSkyMapUpdateSensorCenter"
                @reset-sensor-center="onSkyMapResetSensorCenter"
                @update-obs-time="emit('update-obs-time', $event)"
            />

            <!-- Top-Left Quick Sky Context Button (in Framing mode) -->
            <button
                v-if="!showSkyMap && object"
                type="button"
                class="viewport-sky-map-pill"
                @click="openSkyMap()"
                title="Zoom out to wide-field Sky Map with constellations, stars, and downloaded photos (or scroll out)"
            >
                <span>🌌</span>
                <span>Sky Map</span>
            </button>

            <!-- Top Indeterminate Progress Bar While Fetching Survey -->
            <div
                v-if="!showSkyMap && isFetchingSurvey"
                class="viewport-fetching-bar"
                role="progressbar"
                aria-label="Fetching sky survey image"
            >
                <div class="viewport-fetching-bar-fill"></div>
            </div>

            <!-- Prominent Viewport Fetching / Preview Status Banner -->
            <div
                v-if="!showSkyMap && object && (isFetchingSurvey || (hasAtlasPreview && !isHdReady) || object.status === 'error')"
                class="viewport-status-banner"
                :class="{
                    'is-fetching': isFetchingSurvey,
                    'is-highres': isFetchingHighRes,
                    'is-error': !isFetchingSurvey && object.status === 'error',
                    'is-preview': !isFetchingSurvey && object.status !== 'error'
                }"
            >
                <template v-if="isFetchingSurvey">
                    <span class="status-banner-spinner" aria-hidden="true"></span>
                    <div class="status-banner-copy">
                        <span class="status-banner-title">
                            {{
                                isFetchingHighRes
                                    ? `Fetching High-Res Survey (${activeFetchingResolution}px)…`
                                    : `Fetching Full Sky Survey (${activeFetchingResolution}px)…`
                            }}
                        </span>
                        <span v-if="hasAtlasPreview" class="status-banner-sub">
                            Showing Sky Atlas crop ({{ effectiveDisplayFov.toFixed(2) }}°) · Full image replaces preview automatically
                        </span>
                        <span v-else class="status-banner-sub">
                            Requesting {{ effectiveDisplayFov.toFixed(2) }}° field from survey server…
                        </span>
                    </div>
                </template>

                <template v-else-if="object.status === 'error'">
                    <span class="status-banner-badge error">!</span>
                    <div class="status-banner-copy">
                        <span class="status-banner-title">Survey download failed</span>
                        <span v-if="hasAtlasPreview" class="status-banner-sub">
                            Showing Sky Atlas preview ({{ effectiveDisplayFov.toFixed(2) }}°)
                        </span>
                    </div>
                    <button
                        type="button"
                        class="status-banner-action"
                        @click="updateImage()"
                    >
                        ↻ Retry
                    </button>
                </template>

                <template v-else>
                    <span class="status-banner-badge preview">PREVIEW</span>
                    <div class="status-banner-copy">
                        <span class="status-banner-title">
                            Sky Atlas Crop ({{ effectiveDisplayFov.toFixed(2) }}°)
                        </span>
                        <span class="status-banner-sub">
                            Instant local preview · Fetch survey for full sharpness
                        </span>
                    </div>
                    <button
                        type="button"
                        class="status-banner-action"
                        @click="updateImage()"
                        title="Download full-resolution sky survey for this FOV"
                    >
                        ↻ Load Survey
                    </button>
                </template>
            </div>

            <div
                v-show="!showSkyMap"
                class="image-wrapper"
                :style="{
                    width: wrapperSize + 'px',
                    height: wrapperSize + 'px',
                    transform: `scale(${zoomLevel})`
                }"
            >
                <div class="image-container">
                    <canvas
                        v-show="hasAtlasPreview"
                        ref="atlasPreviewCanvasRef"
                        width="512"
                        height="512"
                        class="sky-image sky-atlas-preview-canvas"
                    ></canvas>
                    <img
                        v-if="object.image_url"
                        ref="hdImgRef"
                        :src="object.image_url"
                        class="sky-image sky-hd-image"
                        :class="{
                            'is-hd-ready': isHdReady,
                            'has-atlas-underlay': hasAtlasPreview
                        }"
                        @load="onHdImageLoad"
                        @error="onHdImageError"
                    />
                    <div v-if="!object.image_url && !hasAtlasPreview" class="sky-image placeholder-text">
                        <span>No Image Loaded</span>
                    </div>
                </div>

                <!-- FOV Object Circles & Labels Overlay -->
                <div
                    v-if="showOverlays && (!isObjectDownloading || hasAtlasPreview) && fovOverlayObjects.length > 0"
                    class="object-overlays-layer"
                >
                    <div
                        v-for="marker in fovOverlayObjects"
                        :key="marker.key"
                        class="fov-object-marker"
                        :class="{
                            'is-primary': marker.isPrimary,
                            'is-sub': marker.isSubObject && !marker.isPrimary,
                            'is-neighbor': !marker.isPrimary && !marker.isSubObject,
                            'is-clickable': Boolean(marker.catalogRef && !marker.isPrimary)
                        }"
                        :style="{
                            left: `${marker.xPct}%`,
                            top: `${marker.yPct}%`,
                            width: `${marker.diameterPct}%`,
                            height: `${marker.diameterPct}%`
                        }"
                    >
                        <div class="fov-object-circle"></div>
                        <div
                            class="fov-object-label"
                            :class="{
                                'label-below': marker.yPct - marker.diameterPct / 2 < 10
                            }"
                            :title="
                                [
                                    marker.name,
                                    marker.commonName ? `(${marker.commonName})` : '',
                                    marker.aliases ? `[≡ ${marker.aliases}]` : '',
                                    marker.majArcmin ? `${marker.majArcmin.toFixed(1)}'` : ''
                                ]
                                    .filter(Boolean)
                                    .join(' ')
                            "
                            @click.stop="onOverlayObjectClick(marker)"
                        >
                            <span class="fov-label-id">{{ marker.name }}</span>
                            <span v-if="marker.commonName" class="fov-label-common">
                                · {{ marker.commonName }}
                            </span>
                        </div>
                    </div>
                </div>

                <div v-if="isObjectDownloading && !hasAtlasPreview" class="download-overlay">
                    <div class="spinner"></div>
                    <span>Downloading sky survey ({{ activeFetchingResolution }}px)...</span>
                </div>

                <!-- Comparison Sensor Rectangles for Other Saved Setups -->
                <template v-if="object && settings && (!isObjectDownloading || hasAtlasPreview)">
                    <div
                        v-for="comp in activeComparisonRectangles"
                        :key="`comp-rect-${comp.name}`"
                        :style="comp.style"
                    >
                        <div
                            class="comparison-sensor-label"
                            :style="{
                                borderColor: comp.color.border,
                                color: comp.color.border
                            }"
                        >
                            <span>{{ comp.name }}</span>
                            <span class="comp-fov-dim">{{ comp.sensorFov.w.toFixed(2) }}°×{{ comp.sensorFov.h.toFixed(2) }}°</span>
                        </div>
                    </div>
                </template>

                <div
                    v-if="object && settings && (!isObjectDownloading || hasAtlasPreview)"
                    :style="sensorStyle"
                    @mousedown="startDrag"
                    @touchstart.stop.prevent="startTouchDrag"
                    title="Drag to re-center framing (Shift + Scroll to rotate)"
                >
                    <div class="crosshair">+</div>
                </div>
            </div>

            <!-- Top-Right Telemetry Readout -->
            <div v-show="!showSkyMap" class="osd-overlay">
                <div class="osd-title">
                    <span class="osd-title-lbl">Profile:</span>
                    <strong class="osd-title-val">{{ settings?.active_profile || 'Custom' }}</strong>
                </div>
                <div class="osd-row" title="Field of View of your selected Telescope + Camera sensor (red frame)">
                    <span class="osd-dot optics"></span>
                    <span class="osd-lbl">Selected Optics:</span>
                    <strong class="optics-val">{{ systemSensorFov.w.toFixed(2) }}° × {{ systemSensorFov.h.toFixed(2) }}°</strong>
                </div>
                <div
                    v-for="comp in activeComparisonRectangles"
                    :key="`osd-comp-${comp.name}`"
                    :title="`Comparison FOV for saved profile '${comp.name}'`"
                    class="osd-row"
                >
                    <span
                        class="osd-dot"
                        :style="{
                            border: `2px dashed ${comp.color.border}`,
                            background: comp.color.bg
                        }"
                    ></span>
                    <span class="osd-lbl">{{ comp.name }}:</span>
                    <strong :style="{ color: comp.color.border }">
                        {{ comp.sensorFov.w.toFixed(2) }}° × {{ comp.sensorFov.h.toFixed(2) }}°
                    </strong>
                </div>
                <div class="osd-row" title="Field of View of the currently displayed sky survey image">
                    <span class="osd-dot view"></span>
                    <span class="osd-lbl">Current View:</span>
                    <strong class="view-val">{{ effectiveDisplayFov.toFixed(2) }}° × {{ effectiveDisplayFov.toFixed(2) }}°</strong>
                </div>
                <div class="osd-row" title="Source and pixel resolution of the currently displayed sky image">
                    <span class="osd-lbl">Image Source:</span>
                    <strong :class="isHdReady ? (isHighResLoaded ? 'hd-res-val' : 'survey-res-val') : 'preview-res-val'">
                        {{
                            isHdReady
                                ? `Survey ${currentImageResolution}px`
                                : hasAtlasPreview
                                    ? 'Atlas Crop Preview'
                                    : isFetchingSurvey
                                        ? 'Fetching…'
                                        : 'None'
                        }}
                    </strong>
                </div>
                <div v-if="isZoomModified" class="osd-row">
                    <span class="osd-lbl">Canvas Zoom:</span>
                    <strong>{{ zoomLevel.toFixed(1) }}×</strong>
                </div>
            </div>

            <!-- Bottom Floating Observing Date Console + Canvas HUD Stack -->
            <div v-show="!showSkyMap" class="framing-bottom-hud-stack">
                <ObservingDateConsole
                    v-if="hasObserverLocation"
                    variant="framing"
                    :is-live-time="isLiveObsTime"
                    :live-now-ms="liveNowMs"
                    :effective-time-ms="effectiveObsTimeMs"
                    :object="object"
                    :settings="settings"
                    :night-times="nightTimes"
                    @update-time="emit('update-obs-time', $event)"
                />

                <!-- Bottom Floating Canvas HUD Toolbar -->
                <div class="canvas-hud" @wheel.stop>
                    <!-- Selected Optics Readout -->
                    <div
                        class="hud-group hud-optics-readout"
                        title="Selected Optics (Telescope + Camera sensor rectangle)"
                    >
                    <span class="hud-label">OPTICS</span>
                    <span class="hud-optics-badge">
                        {{ systemSensorFov.w.toFixed(2) }}° × {{ systemSensorFov.h.toFixed(2) }}°
                    </span>
                </div>

                <div class="hud-divider"></div>

                <!-- Current View FOV Control -->
                <div class="hud-group" title="Current View FOV (sky survey image width/height in degrees)">
                    <span class="hud-label hud-label-full">CURRENT VIEW (°)</span>
                    <span class="hud-label hud-label-short">FOV</span>
                    <button
                        type="button"
                        class="hud-step-btn"
                        @click="stepFov(0.9)"
                        title="Decrease Current View FOV (Zoom In)"
                    >
                        −
                    </button>
                    <input
                        type="number"
                        v-model.number="localFov"
                        min="0.25"
                        step="0.05"
                        class="hud-input"
                        @change="onFovInputChange"
                        @keyup.enter="updateImage()"
                    />
                    <button
                        type="button"
                        class="hud-step-btn"
                        @click="stepFov(1.1)"
                        title="Increase Current View FOV (Zoom Out)"
                    >
                        +
                    </button>
                </div>

                <div class="hud-divider"></div>

                <!-- Rotation Control -->
                <div class="hud-group">
                    <span class="hud-label hud-label-full">ROT (°)</span>
                    <span class="hud-label hud-label-short">ROT</span>
                    <button
                        type="button"
                        class="hud-step-btn"
                        @mousedown="startRotate(-1)"
                        @mouseup="stopRotate"
                        @mouseleave="stopRotate"
                        @touchstart.prevent="startRotate(-1)"
                        @touchend="stopRotate"
                        @touchcancel="stopRotate"
                        title="Rotate Counter-Clockwise"
                    >
                        ⟲
                    </button>
                    <input type="number" v-model.number="rotation" step="1" class="hud-input" />
                    <button
                        type="button"
                        class="hud-step-btn"
                        @mousedown="startRotate(1)"
                        @mouseup="stopRotate"
                        @mouseleave="stopRotate"
                        @touchstart.prevent="startRotate(1)"
                        @touchend="stopRotate"
                        @touchcancel="stopRotate"
                        title="Rotate Clockwise"
                    >
                        ⟳
                    </button>
                </div>

                <div class="hud-divider"></div>

                <!-- Quick Canvas Actions -->
                <div class="hud-actions">
                    <!-- Compare Saved Setups Popover -->
                    <div
                        v-if="availableComparisonProfiles.length > 0"
                        class="setups-popover-wrapper"
                        ref="setupsMenuRef"
                    >
                        <button
                            type="button"
                            class="hud-btn"
                            :class="{ 'toggle-active': activeComparisonRectangles.length > 0 || showSetupsMenu }"
                            @click.stop="showSetupsMenu = !showSetupsMenu"
                            title="Overlay differently colored FOV rectangles for other saved equipment setups"
                        >
                            ⧉ Setups<span v-if="activeComparisonRectangles.length > 0"> ({{ activeComparisonRectangles.length }})</span>
                        </button>

                        <div v-if="showSetupsMenu" class="setups-popover" @click.stop>
                            <div class="setups-popover-header">
                                <span class="setups-popover-title">Compare Saved Setups</span>
                                <div class="setups-popover-bulk">
                                    <button
                                        type="button"
                                        class="popover-link-btn"
                                        @click="setAllComparisonProfiles(true)"
                                    >
                                        All
                                    </button>
                                    <span class="meta-sep">·</span>
                                    <button
                                        type="button"
                                        class="popover-link-btn"
                                        @click="setAllComparisonProfiles(false)"
                                    >
                                        None
                                    </button>
                                </div>
                            </div>

                            <div class="setups-popover-list">
                                <label
                                    v-for="prof in availableComparisonProfiles"
                                    :key="`menu-prof-${prof.name}`"
                                    class="setups-popover-item"
                                    :class="{ 'is-enabled': prof.enabled }"
                                >
                                    <input
                                        type="checkbox"
                                        :checked="prof.enabled"
                                        @change="toggleComparisonProfile(prof.name)"
                                    />
                                    <span
                                        class="setup-color-swatch"
                                        :style="{
                                            borderColor: prof.color.border,
                                            background: prof.color.bg
                                        }"
                                    ></span>
                                    <span class="setup-item-name" :title="prof.name">{{ prof.name }}</span>
                                    <span
                                        class="setup-item-fov"
                                        :style="{ color: prof.color.border }"
                                    >
                                        {{ prof.sensorFov.w.toFixed(2) }}° × {{ prof.sensorFov.h.toFixed(2) }}°
                                    </span>
                                </label>
                            </div>
                        </div>
                    </div>

                    <button
                        type="button"
                        class="hud-btn"
                        @click="openSkyMap()"
                        title="Zoom out to wide-field Sky Map with constellations, stars, and downloaded photos"
                    >
                        🌌 Sky Map
                    </button>

                    <button
                        type="button"
                        class="hud-btn"
                        :class="{ 'toggle-active': showOverlays }"
                        @click="toggleOverlays"
                        title="Toggle object circle & label overlays in the current Field of View"
                    >
                        ◎ Objects: {{ showOverlays ? 'ON' : 'OFF' }}
                    </button>

                    <button
                        type="button"
                        class="hud-btn primary"
                        :class="{ 'modified-warning': isModified }"
                        :disabled="isObjectDownloading"
                        @click="updateImage()"
                        title="Download new sky survey centered on current sensor rectangle and FOV"
                    >
                        {{
                            isObjectDownloading && !isFetchingHighRes
                                ? 'Fetching...'
                                : isModified
                                    ? '↻ Re-center & Fetch'
                                    : '↻ Refresh Image'
                        }}
                    </button>

                    <button
                        type="button"
                        class="hud-btn highres-btn"
                        :class="{
                            'is-hd-loaded': isHighResLoaded,
                            'is-hd-fetching': isFetchingHighRes
                        }"
                        :disabled="isObjectDownloading"
                        @click="downloadHighResImage"
                        :title="
                            isHighResLoaded
                                ? `High-resolution ${currentImageResolution}px survey image is active (click to re-fetch)`
                                : `Download a sharper ${highResTarget}×${highResTarget}px sky survey image for the current view`
                        "
                    >
                        <template v-if="isFetchingHighRes">
                            ✦ Fetching {{ activeFetchingResolution }}px…
                        </template>
                        <template v-else-if="isHighResLoaded">
                            ✦ HD {{ currentImageResolution }}px ✓
                        </template>
                        <template v-else>
                            ✦ High-Res ({{ highResTarget }}px)
                        </template>
                    </button>

                    <button
                        v-if="isCustomFov"
                        type="button"
                        class="hud-btn warning"
                        @click="resetFov"
                        :title="`Reset FOV to match telescope/camera setup (${defaultImageFov.toFixed(2)}°)`"
                    >
                        Reset FOV
                    </button>

                    <button
                        v-if="isZoomModified"
                        type="button"
                        class="hud-btn"
                        @click="resetZoom"
                        title="Reset viewport zoom to 1x"
                    >
                        1:1 Zoom
                    </button>
                </div>
                </div>
            </div>
        </div>
    </article>
</template>

<style scoped>
.framing-panel {
    height: 100%;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    background: #000;
    margin: 0 !important;
    padding: 0 !important;
    border: none;
    border-radius: 0;
}

.framing-header {
    margin: 0 !important;
    padding: 10px 16px !important;
    background: #1f2937;
    border-bottom: 1px solid #374151;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
}

.target-identity {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
}

.title-line {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
}

.title-line h2 {
    margin: 0;
    font-size: 1.25rem;
    font-weight: 700;
    color: #f9fafb;
}

.fav-star-btn {
    background: rgba(31, 41, 55, 0.8);
    border: 1px solid #4b5563;
    color: #9ca3af;
    width: 28px;
    height: 28px;
    border-radius: 6px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 1.05rem;
    line-height: 1;
    padding: 0;
    margin: 0;
    cursor: pointer;
    transition: all 0.15s;
}

.fav-star-btn:hover {
    border-color: #f59e0b;
    color: #fbbf24;
    background: rgba(245, 158, 11, 0.12);
}

.fav-star-btn.is-fav {
    border-color: rgba(245, 158, 11, 0.65);
    color: #fbbf24;
    background: rgba(245, 158, 11, 0.18);
    text-shadow: 0 0 8px rgba(245, 158, 11, 0.45);
}

.meta-badge {
    font-size: 0.7rem;
    background: #374151;
    color: #d1d5db;
    padding: 2px 7px;
    border-radius: 4px;
    font-weight: 600;
    text-transform: uppercase;
}

.meta-badge.accent {
    background: rgba(16, 185, 129, 0.15);
    color: #10b981;
    border: 1px solid rgba(16, 185, 129, 0.3);
}

.aliases-badge {
    font-size: 0.72rem;
    background: rgba(96, 165, 250, 0.12);
    color: #93c5fd;
    border: 1px solid rgba(96, 165, 250, 0.28);
    padding: 2px 8px;
    border-radius: 4px;
    font-weight: 500;
    max-width: 420px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.subtitle-line {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 0.78rem;
    color: #9ca3af;
    flex-wrap: wrap;
}

.subtitle-line strong {
    color: #e5e7eb;
}

.common-title {
    color: #60a5fa;
    font-weight: 600;
}

.meta-sep {
    color: #4b5563;
}

.header-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
}

.nina-btn {
    display: flex;
    align-items: center;
    gap: 6px;
    background: #10b981;
    border: 1px solid #059669;
    color: #064e3b;
    font-weight: 700;
    font-size: 0.82rem;
    padding: 6px 12px;
    border-radius: 6px;
    cursor: pointer;
    margin: 0;
    transition: all 0.15s;
}

.nina-btn:hover {
    background: #34d399;
}

/* Viewport */
.framing-viewport {
    flex: 1;
    background: #000;
    position: relative;
    overflow: hidden;
    display: flex;
    justify-content: center;
    align-items: center;
}

.image-wrapper {
    position: relative;
    display: flex;
    justify-content: center;
    align-items: center;
    background: #000;
}

.image-container {
    position: relative;
    width: 100%;
    height: 100%;
    display: flex;
    justify-content: center;
    align-items: center;
    overflow: hidden;
}

.sky-image {
    width: 100%;
    height: 100%;
    object-fit: contain;
    user-select: none;
    -webkit-user-drag: none;
}

.sky-atlas-preview-canvas {
    position: absolute;
    inset: 0;
    z-index: 1;
    filter: contrast(1.03);
}

.sky-hd-image {
    position: relative;
    z-index: 2;
}

.sky-hd-image.has-atlas-underlay {
    position: absolute;
    inset: 0;
    opacity: 0;
    transition: opacity 0.18s ease-out;
}

.sky-hd-image.has-atlas-underlay.is-hd-ready {
    opacity: 1;
}

/* Top Indeterminate Progress Shimmer Bar */
.viewport-fetching-bar {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 3.5px;
    background: rgba(15, 23, 42, 0.75);
    overflow: hidden;
    z-index: 120;
    pointer-events: none;
}

.viewport-fetching-bar-fill {
    position: absolute;
    top: 0;
    bottom: 0;
    width: 42%;
    background: linear-gradient(90deg, transparent 0%, #38bdf8 28%, #fbbf24 62%, #34d399 88%, transparent 100%);
    box-shadow: 0 0 12px rgba(251, 191, 36, 0.75);
    animation: viewportFetchSlide 1.35s cubic-bezier(0.4, 0, 0.2, 1) infinite;
}

@keyframes viewportFetchSlide {
    0% {
        left: -45%;
    }
    100% {
        left: 105%;
    }
}

/* Prominent Viewport Fetching / Preview Status Banner */
.viewport-status-banner {
    position: absolute;
    top: 14px;
    left: 50%;
    transform: translateX(-50%);
    z-index: 110;
    display: inline-flex;
    align-items: center;
    gap: 11px;
    padding: 8px 16px;
    border-radius: 10px;
    background: rgba(10, 16, 30, 0.93);
    backdrop-filter: blur(10px);
    border: 1.5px solid rgba(56, 189, 248, 0.65);
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.78), 0 0 18px rgba(56, 189, 248, 0.22);
    max-width: min(560px, calc(100% - 28px));
    pointer-events: auto;
}

.viewport-status-banner.is-fetching {
    border-color: rgba(251, 191, 36, 0.85);
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.82), 0 0 22px rgba(245, 158, 11, 0.32);
    animation: statusBannerPulse 2s ease-in-out infinite;
}

.viewport-status-banner.is-fetching.is-highres {
    border-color: rgba(168, 85, 247, 0.88);
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.82), 0 0 22px rgba(168, 85, 247, 0.36);
}

.viewport-status-banner.is-error {
    border-color: rgba(239, 68, 68, 0.82);
    box-shadow: 0 10px 28px rgba(0, 0, 0, 0.82), 0 0 18px rgba(239, 68, 68, 0.28);
}

@keyframes statusBannerPulse {
    0%, 100% {
        box-shadow: 0 10px 28px rgba(0, 0, 0, 0.82), 0 0 16px rgba(245, 158, 11, 0.24);
    }
    50% {
        box-shadow: 0 10px 30px rgba(0, 0, 0, 0.85), 0 0 26px rgba(251, 191, 36, 0.48);
    }
}

.status-banner-spinner {
    width: 18px;
    height: 18px;
    border-radius: 50%;
    border: 2.5px solid rgba(251, 191, 36, 0.25);
    border-top-color: #fbbf24;
    border-right-color: #38bdf8;
    animation: spin 0.8s linear infinite;
    flex-shrink: 0;
}

.viewport-status-banner.is-highres .status-banner-spinner {
    border-color: rgba(192, 132, 252, 0.25);
    border-top-color: #e879f9;
    border-right-color: #38bdf8;
}

.status-banner-badge {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 2px 7px;
    border-radius: 5px;
    font-size: 0.66rem;
    font-weight: 800;
    letter-spacing: 0.05em;
    flex-shrink: 0;
}

.status-banner-badge.preview {
    background: rgba(56, 189, 248, 0.2);
    border: 1px solid rgba(56, 189, 248, 0.55);
    color: #7dd3fc;
}

.status-banner-badge.error {
    width: 20px;
    height: 20px;
    padding: 0;
    border-radius: 50%;
    background: rgba(239, 68, 68, 0.24);
    border: 1px solid rgba(239, 68, 68, 0.7);
    color: #fca5a5;
    font-size: 0.78rem;
}

.status-banner-copy {
    display: flex;
    flex-direction: column;
    gap: 1px;
    min-width: 0;
}

.status-banner-title {
    color: #fef3c7;
    font-size: 0.82rem;
    font-weight: 700;
    letter-spacing: 0.01em;
    line-height: 1.25;
}

.viewport-status-banner.is-preview .status-banner-title {
    color: #e0f2fe;
}

.viewport-status-banner.is-highres .status-banner-title {
    color: #f3e8ff;
}

.viewport-status-banner.is-error .status-banner-title {
    color: #fecaca;
}

.status-banner-sub {
    color: #cbd5e1;
    font-size: 0.71rem;
    font-weight: 500;
    line-height: 1.25;
}

.status-banner-action {
    margin: 0 0 0 4px;
    padding: 4px 10px;
    height: 26px;
    border-radius: 6px;
    border: 1px solid #38bdf8;
    background: rgba(56, 189, 248, 0.18);
    color: #e0f2fe;
    font-size: 0.73rem;
    font-weight: 700;
    white-space: nowrap;
    cursor: pointer;
    flex-shrink: 0;
    transition: background 0.15s, border-color 0.15s;
}

.status-banner-action:hover {
    background: rgba(56, 189, 248, 0.32);
    border-color: #7dd3fc;
}

.placeholder-text {
    display: flex;
    justify-content: center;
    align-items: center;
    color: #6b7280;
    font-size: 1.1rem;
    border: 1px dashed #374151;
}

.crosshair {
    position: absolute;
    left: 50%;
    top: 50%;
    transform: translate(-50%, -50%);
    color: #ef4444;
    pointer-events: none;
    font-size: 20px;
}

.comparison-sensor-label {
    position: absolute;
    top: 4px;
    left: 4px;
    background: rgba(15, 23, 42, 0.86);
    backdrop-filter: blur(4px);
    border: 1px solid;
    border-radius: 4px;
    padding: 1px 6px;
    font-size: 0.66rem;
    font-weight: 700;
    line-height: 1.25;
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    pointer-events: none;
    user-select: none;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.65);
}

.comp-fov-dim {
    font-weight: 500;
    opacity: 0.9;
    font-variant-numeric: tabular-nums;
}

/* Compare Setups Popover */
.setups-popover-wrapper {
    position: relative;
    display: inline-flex;
}

.setups-popover {
    position: absolute;
    bottom: calc(100% + 10px);
    left: 50%;
    transform: translateX(-50%);
    min-width: 245px;
    max-width: 320px;
    background: rgba(15, 23, 42, 0.96);
    backdrop-filter: blur(10px);
    border: 1px solid #475569;
    border-radius: 8px;
    padding: 8px 10px;
    box-shadow: 0 10px 25px rgba(0, 0, 0, 0.75);
    z-index: 150;
    display: flex;
    flex-direction: column;
    gap: 6px;
}

.setups-popover-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding-bottom: 5px;
    border-bottom: 1px solid rgba(71, 85, 105, 0.6);
}

.setups-popover-title {
    font-size: 0.72rem;
    font-weight: 700;
    color: #e2e8f0;
    text-transform: uppercase;
    letter-spacing: 0.03em;
}

.setups-popover-bulk {
    display: inline-flex;
    align-items: center;
    gap: 4px;
}

.popover-link-btn {
    background: transparent;
    border: none;
    color: #38bdf8;
    font-size: 0.7rem;
    font-weight: 600;
    padding: 0 2px;
    margin: 0;
    cursor: pointer;
}

.popover-link-btn:hover {
    text-decoration: underline;
    color: #7dd3fc;
}

.setups-popover-list {
    display: flex;
    flex-direction: column;
    gap: 4px;
    max-height: 200px;
    overflow-y: auto;
}

.setups-popover-item {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 4px 6px;
    border-radius: 5px;
    cursor: pointer;
    font-size: 0.75rem;
    color: #cbd5e1;
    margin: 0;
    user-select: none;
    transition: background 0.12s;
}

.setups-popover-item:hover {
    background: rgba(51, 65, 85, 0.55);
}

.setups-popover-item.is-enabled {
    background: rgba(30, 41, 59, 0.85);
    color: #f8fafc;
}

.setups-popover-item input[type="checkbox"] {
    margin: 0;
    width: 14px;
    height: 14px;
    cursor: pointer;
    accent-color: #38bdf8;
}

.setup-color-swatch {
    width: 10px;
    height: 10px;
    border: 2px dashed;
    border-radius: 2px;
    flex-shrink: 0;
}

.setup-item-name {
    flex: 1;
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.setup-item-fov {
    font-size: 0.7rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
}

/* FOV Object Circles & Labels Overlay */
.object-overlays-layer {
    position: absolute;
    inset: 0;
    pointer-events: none;
    z-index: 12;
    overflow: hidden;
}

.fov-object-marker {
    position: absolute;
    transform: translate(-50%, -50%);
    pointer-events: none;
}

.fov-object-circle {
    width: 100%;
    height: 100%;
    border-radius: 50%;
    box-sizing: border-box;
    pointer-events: none;
    transition: border-color 0.15s, box-shadow 0.15s;
}

.fov-object-marker.is-primary .fov-object-circle {
    border: 1.8px solid rgba(16, 185, 129, 0.9);
    box-shadow: 0 0 10px rgba(16, 185, 129, 0.35), inset 0 0 8px rgba(16, 185, 129, 0.12);
}

.fov-object-marker.is-sub .fov-object-circle {
    border: 1.5px dashed rgba(56, 189, 248, 0.85);
    box-shadow: 0 0 6px rgba(56, 189, 248, 0.25);
}

.fov-object-marker.is-neighbor .fov-object-circle {
    border: 1.5px solid rgba(96, 165, 250, 0.85);
    box-shadow: 0 0 8px rgba(96, 165, 250, 0.3);
}

.fov-object-label {
    position: absolute;
    left: 50%;
    bottom: calc(100% + 4px);
    transform: translateX(-50%);
    background: rgba(15, 23, 42, 0.82);
    backdrop-filter: blur(4px);
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 0.68rem;
    line-height: 1.25;
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 3px;
    pointer-events: auto;
    user-select: none;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.6);
}

.fov-object-label.label-below {
    bottom: auto;
    top: calc(100% + 4px);
}

.fov-object-marker.is-primary .fov-object-label {
    border: 1px solid rgba(16, 185, 129, 0.6);
    color: #ecfdf5;
}

.fov-object-marker.is-primary .fov-label-id {
    color: #34d399;
    font-weight: 700;
}

.fov-object-marker.is-sub .fov-object-label {
    border: 1px solid rgba(56, 189, 248, 0.5);
    color: #e0f2fe;
}

.fov-object-marker.is-sub .fov-label-id {
    color: #38bdf8;
    font-weight: 600;
}

.fov-object-marker.is-neighbor .fov-object-label {
    border: 1px solid rgba(96, 165, 250, 0.55);
    color: #dbeafe;
}

.fov-object-marker.is-neighbor .fov-label-id {
    color: #60a5fa;
    font-weight: 600;
}

.fov-object-marker.is-clickable .fov-object-label {
    cursor: pointer;
}

.fov-object-marker.is-clickable .fov-object-label:hover {
    background: rgba(30, 41, 59, 0.95);
    border-color: #93c5fd;
}

.fov-label-common {
    color: #cbd5e1;
    font-weight: 400;
    max-width: 130px;
    overflow: hidden;
    text-overflow: ellipsis;
}

/* Top-Right Telemetry Overlay */
.osd-overlay {
    position: absolute;
    top: 12px;
    right: 12px;
    background: rgba(15, 23, 42, 0.85);
    backdrop-filter: blur(6px);
    border: 1px solid rgba(51, 65, 85, 0.85);
    border-radius: 7px;
    color: #94a3b8;
    font-size: 0.74rem;
    padding: 7px 11px;
    pointer-events: none;
    z-index: 90;
    display: flex;
    flex-direction: column;
    gap: 3px;
    line-height: 1.35;
}

.osd-title {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 5px;
    padding-bottom: 4px;
    margin-bottom: 2px;
    border-bottom: 1px solid rgba(71, 85, 105, 0.55);
}

.osd-title-lbl {
    color: #94a3b8;
    font-weight: 600;
}

.osd-overlay strong.osd-title-val {
    color: #f8fafc;
    font-weight: 700;
}

.osd-row {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 6px;
}

.osd-dot {
    width: 8px;
    height: 8px;
    border-radius: 2px;
    display: inline-block;
    flex-shrink: 0;
}

.osd-dot.optics {
    border: 2px solid #10b981;
    background: rgba(16, 185, 129, 0.2);
}

.osd-dot.view {
    border: 2px solid #38bdf8;
    background: rgba(56, 189, 248, 0.2);
}

.osd-lbl {
    color: #94a3b8;
}

.osd-overlay strong {
    color: #10b981;
    font-variant-numeric: tabular-nums;
}

.osd-overlay strong.view-val {
    color: #38bdf8;
}

.osd-overlay strong.survey-res-val {
    color: #34d399;
}

.osd-overlay strong.hd-res-val {
    color: #e879f9;
}

.osd-overlay strong.preview-res-val {
    color: #fbbf24;
}

.hud-optics-badge {
    background: rgba(16, 185, 129, 0.12);
    border: 1px solid rgba(16, 185, 129, 0.35);
    color: #34d399;
    font-size: 0.78rem;
    font-weight: 700;
    height: 28px;
    padding: 0 9px;
    border-radius: 4px;
    display: inline-flex;
    align-items: center;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
}

/* Bottom Floating Observing Date Console + HUD Toolbar Stack */
.framing-bottom-hud-stack {
    position: absolute;
    bottom: 14px;
    left: 50%;
    transform: translateX(-50%);
    z-index: 100;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    width: max-content;
    max-width: 95%;
    pointer-events: none;
}

.framing-bottom-hud-stack > * {
    pointer-events: auto;
}

/* Bottom Floating HUD Toolbar */
.canvas-hud {
    position: relative;
    background: rgba(17, 24, 39, 0.88);
    backdrop-filter: blur(8px);
    border: 1px solid #374151;
    border-radius: 10px;
    padding: 6px 12px;
    display: flex;
    align-items: center;
    gap: 10px;
    box-shadow: 0 8px 20px rgba(0, 0, 0, 0.6);
    max-width: 100%;
    flex-wrap: wrap;
    justify-content: center;
}

.hud-group {
    display: flex;
    align-items: center;
    gap: 4px;
}

.hud-label {
    font-size: 0.7rem;
    font-weight: 700;
    color: #9ca3af;
    margin-right: 4px;
    white-space: nowrap;
}

.hud-step-btn {
    background: #1f2937;
    border: 1px solid #4b5563;
    color: #e5e7eb;
    width: 26px;
    height: 28px;
    border-radius: 4px;
    font-size: 0.85rem;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    padding: 0;
    margin: 0;
}

.hud-step-btn:hover {
    background: #374151;
    color: #fff;
}

.hud-input {
    width: 62px;
    height: 28px;
    background: #0f172a;
    border: 1px solid #4b5563;
    color: #fff;
    border-radius: 4px;
    font-size: 0.82rem;
    text-align: center;
    padding: 0 4px;
    margin: 0;
    box-sizing: border-box;
}

.hud-divider {
    width: 1px;
    height: 22px;
    background: #374151;
}

.hud-actions {
    display: flex;
    align-items: center;
    gap: 6px;
}

.hud-btn {
    background: #1f2937;
    border: 1px solid #4b5563;
    color: #e5e7eb;
    font-size: 0.75rem;
    font-weight: 600;
    height: 28px;
    padding: 0 10px;
    border-radius: 5px;
    cursor: pointer;
    margin: 0;
    white-space: nowrap;
    transition: all 0.15s;
}

.hud-btn:hover {
    background: #374151;
}

.hud-btn:disabled {
    opacity: 0.65;
    cursor: wait;
}

.hud-btn.primary {
    border-color: #10b981;
    color: #10b981;
}

.hud-btn.highres-btn {
    border-color: rgba(168, 85, 247, 0.72);
    color: #e9d5ff;
    background: rgba(126, 34, 206, 0.18);
}

.hud-btn.highres-btn:hover:not(:disabled) {
    background: rgba(147, 51, 234, 0.32);
    border-color: #c084fc;
    color: #faf5ff;
}

.hud-btn.highres-btn.is-hd-loaded {
    border-color: #c084fc;
    color: #f5d0fe;
    background: rgba(168, 85, 247, 0.28);
}

.hud-btn.highres-btn.is-hd-fetching {
    border-color: #e879f9;
    color: #fae8ff;
    background: rgba(192, 38, 211, 0.3);
}

.hud-btn.toggle-active {
    border-color: #38bdf8;
    color: #38bdf8;
    background: rgba(56, 189, 248, 0.14);
}

.hud-btn.warning,
.hud-btn.modified-warning {
    border-color: #ef4444 !important;
    color: #fca5a5 !important;
    background: rgba(239, 68, 68, 0.18) !important;
}

.download-overlay {
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.7);
    color: #10b981;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    z-index: 20;
}

.spinner {
    border: 4px solid #374151;
    border-top: 4px solid #10b981;
    border-radius: 50%;
    width: 30px;
    height: 30px;
    animation: spin 1s linear infinite;
    margin-bottom: 10px;
}

@keyframes spin {
    0% { transform: rotate(0deg); }
    100% { transform: rotate(360deg); }
}

.nina-btn-short {
    display: none;
}

.hud-label-short {
    display: none;
}

@media (max-width: 768px) {
    .framing-header {
        padding: 6px 10px !important;
        gap: 8px;
    }

    .title-line {
        gap: 6px;
    }

    .title-line h2 {
        font-size: 1.05rem;
    }

    .aliases-badge {
        max-width: 170px;
        font-size: 0.65rem;
    }

    .subtitle-line {
        font-size: 0.7rem;
        gap: 4px;
    }

    .nina-btn {
        padding: 5px 9px;
        font-size: 0.75rem;
    }

    .nina-btn-text {
        display: none;
    }

    .nina-btn-short {
        display: inline;
    }

    .osd-overlay {
        top: 8px;
        right: 8px;
        font-size: 0.65rem;
        padding: 4px 8px;
        gap: 2px;
    }

    .viewport-status-banner {
        top: 44px;
        max-width: calc(100% - 16px);
        padding: 5px 10px;
        gap: 7px;
        font-size: 0.72rem;
    }

    .status-banner-sub {
        display: none;
    }

    .osd-title {
        padding-bottom: 2px;
        margin-bottom: 1px;
    }

    .hud-optics-readout {
        display: none;
    }

    .hud-divider {
        display: none;
    }

    .hud-label-full {
        display: none;
    }

    .hud-label-short {
        display: inline;
    }

    .framing-bottom-hud-stack {
        bottom: 8px;
        gap: 6px;
        width: calc(100% - 16px);
        max-width: 440px;
    }

    .canvas-hud {
        padding: 6px 8px;
        gap: 6px 10px;
        width: 100%;
        max-width: 440px;
        box-sizing: border-box;
    }

    .hud-step-btn {
        width: 30px;
        height: 30px;
        font-size: 0.95rem;
    }

    .hud-input {
        width: 54px;
        height: 30px;
        font-size: 0.8rem;
    }

    .hud-actions {
        width: 100%;
        justify-content: center;
        flex-wrap: wrap;
        gap: 5px;
    }

    .hud-btn {
        height: 30px;
        padding: 0 9px;
        font-size: 0.72rem;
        flex: 1 1 auto;
        text-align: center;
    }
}

.viewport-sky-map-pill {
    position: absolute;
    top: 12px;
    left: 12px;
    z-index: 22;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 12px;
    margin: 0;
    border-radius: 7px;
    background: rgba(13, 18, 28, 0.82);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    border: 1px solid rgba(148, 163, 184, 0.28);
    color: #e2e8f0;
    font-size: 0.78rem;
    font-weight: 500;
    line-height: 1.2;
    cursor: pointer;
    transition: all 0.15s ease;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.42);
}

.viewport-sky-map-pill:hover {
    background: rgba(14, 116, 144, 0.34);
    border-color: rgba(56, 189, 248, 0.65);
    color: #ffffff;
}
</style>
