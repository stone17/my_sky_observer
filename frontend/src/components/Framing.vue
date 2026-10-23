<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue';
import {
    computeSensorFov,
    computeDefaultImageFov,
    normalizeImageFov,
    MIN_IMAGE_FOV_DEG
} from '../utils/fov.js';
import {
    getObjectTypeLabel,
    getObjectTypeDescription,
    formatMagnitude
} from '../utils/catalogMeta.js';

const props = defineProps(['object', 'objects', 'settings', 'clientSettings', 'searchQuery', 'activeFov']);
const emit = defineEmits(['update-settings', 'update-client-settings', 'update-search', 'select-object', 'update-fov', 'open-mobile-list']);

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

    const imgFov = parseFloat(props.object.image_fov || localFov.value || defaultImageFov.value);
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

    // 1. Primary selected object
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
        touchAction: 'none'
    };
});

// --- ROTATION & DRAG LOGIC ---
const rotation = ref(0);
let rotInterval = null;
let rotTimeout = null;

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

const offsetX = ref(0);
const offsetY = ref(0);
const isDragging = ref(false);
const dragStart = ref({ x: 0, y: 0 });
const pinchStartDist = ref(0);
const pinchStartZoom = ref(1.0);

const viewportRef = ref(null);
const wrapperSize = ref(0);
let resizeObserver = null;

const zoomLevel = ref(1.0);

watch(() => props.object?.name, (newName, oldName) => {
    if (newName !== oldName) {
        offsetX.value = 0;
        offsetY.value = 0;
        zoomLevel.value = 1.0;
    }
});

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
    offsetX.value = 0;
    offsetY.value = 0;
    zoomLevel.value = 1.0;
    localFov.value = defaultImageFov.value;
    emit('update-fov', { fov: defaultImageFov.value, isReset: true });
};

const onWheel = (e) => {
    if (e.shiftKey) {
        // Shift + Wheel rotates sensor
        const dir = e.deltaY < 0 ? 2 : -2;
        rotation.value = (rotation.value + dir + 360) % 360;
        return;
    }
    if (e.deltaY < 0) {
        zoomLevel.value = Math.min(zoomLevel.value * 1.1, 10.0);
    } else if (e.deltaY > 0) {
        zoomLevel.value = Math.max(zoomLevel.value / 1.1, 0.5);
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
    isDragging.value = true;
    dragStart.value = {
        x: e.clientX - (offsetX.value * zoomLevel.value),
        y: e.clientY - (offsetY.value * zoomLevel.value)
    };
    e.preventDefault();
};
const stopDrag = () => {
    isDragging.value = false;
    pinchStartDist.value = 0;
};

const getTouchDistance = (t1, t2) => {
    return Math.hypot(t2.clientX - t1.clientX, t2.clientY - t1.clientY);
};

const startTouchDrag = (e) => {
    if (e.touches.length === 1) {
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
    if (e.touches.length === 2 && pinchStartDist.value > 0) {
        const dist = getTouchDistance(e.touches[0], e.touches[1]);
        const scale = dist / pinchStartDist.value;
        zoomLevel.value = Math.min(Math.max(pinchStartZoom.value * scale, 0.5), 10.0);
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
    const baseFov = props.object?.image_fov || localFov.value;
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

const isZoomModified = computed(() => Math.abs(zoomLevel.value - 1.0) > 0.01);

const isFovModified = computed(() => {
    if (!props.object) return false;
    const currentImgFov = props.object.image_fov || defaultImageFov.value;
    return Math.abs(localFov.value - currentImgFov) >= 0.01;
});

const isCustomFov = computed(() => {
    const currentImgFov = props.object?.image_fov || localFov.value;
    return (
        Math.abs(localFov.value - defaultImageFov.value) >= 0.01 ||
        Math.abs(currentImgFov - defaultImageFov.value) >= 0.01 ||
        Boolean(props.object?.custom_framed)
    );
});

const isModified = computed(() => {
    if (!props.object) return false;
    if (Math.abs(offsetX.value) > 0.01 || Math.abs(offsetY.value) > 0.01) return true;
    return isFovModified.value;
});

const updateImage = async () => {
    if (!props.object) return;

    const targetFov = normalizeImageFov(localFov.value, defaultImageFov.value);
    localFov.value = targetFov;

    const hasOffset = Math.abs(offsetX.value) > 0.01 || Math.abs(offsetY.value) > 0.01;
    const isOffCenter = hasOffset || Boolean(props.object.custom_framed);

    // If centered on catalog coordinates, use standard object download via App.vue
    if (!isOffCenter) {
        emit('update-fov', { fov: targetFov });
        return;
    }

    const coords = getCurrentCenterCoordinates();

    props.object.status = 'downloading';
    emit('update-fov', { fov: targetFov, skipDownload: true });

    try {
        const res = await fetch('/api/fetch-custom-image', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ra: coords.ra,
                dec: coords.dec,
                fov: targetFov,
                resolution: props.settings.image_server?.resolution || 1024,
                source: props.settings.image_server?.source || "dss2r",
                timeout: props.settings.image_server?.timeout || 120
            })
        });

        if (res.ok) {
            const data = await res.json();
            const uniqueUrl = `${data.url}?t=${new Date().getTime()}`;

            props.object.image_url = uniqueUrl;
            props.object.center_ra = coords.ra;
            props.object.center_dec = coords.dec;
            props.object.custom_framed = true;
            props.object.image_fov = data.image_fov || targetFov;
            props.object.status = 'cached';

            offsetX.value = 0;
            offsetY.value = 0;
            zoomLevel.value = 1.0;
        } else {
            props.object.status = 'error';
        }
    } catch (e) {
        console.error(e);
        props.object.status = 'error';
    }
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
    if (viewportRef.value) {
        resizeObserver = new ResizeObserver(entries => {
            for (let entry of entries) {
                wrapperSize.value = Math.min(entry.contentRect.width, entry.contentRect.height);
            }
        });
        resizeObserver.observe(viewportRef.value);
    }
});
onUnmounted(() => {
    if (resizeObserver) resizeObserver.disconnect();
    stopRotate();
});
</script>

<template>
    <article class="framing-panel">
        <!-- Streamlined Object Info & Primary Action Header -->
        <header class="framing-header">
            <div class="target-identity">
                <div class="title-line">
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
                    <span>Mag: <strong>{{ formatMagnitude(object.mag) }}</strong></span>
                    <span class="meta-sep">•</span>
                    <span>Size: <strong>{{ object.size }}</strong></span>
                    <span class="meta-sep">•</span>
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
            <div
                class="image-wrapper"
                :style="{
                    width: wrapperSize + 'px',
                    height: wrapperSize + 'px',
                    transform: `scale(${zoomLevel})`
                }"
            >
                <div class="image-container">
                    <img v-if="object.image_url" :src="object.image_url" class="sky-image" />
                    <div v-else class="sky-image placeholder-text">
                        <span>No Image Loaded</span>
                    </div>
                </div>

                <!-- FOV Object Circles & Labels Overlay -->
                <div
                    v-if="showOverlays && !isObjectDownloading && fovOverlayObjects.length > 0"
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

                <div v-if="isObjectDownloading" class="download-overlay">
                    <div class="spinner"></div>
                    <span>Downloading sky survey...</span>
                </div>

                <div
                    v-if="object && settings && !isObjectDownloading"
                    :style="sensorStyle"
                    @mousedown="startDrag"
                    @touchstart.stop.prevent="startTouchDrag"
                    title="Drag to re-center framing (Shift + Scroll to rotate)"
                >
                    <div class="crosshair">+</div>
                </div>
            </div>

            <!-- Top-Right Telemetry Readout -->
            <div class="osd-overlay">
                <div class="osd-title">
                    <span class="osd-title-lbl">Profile:</span>
                    <strong class="osd-title-val">{{ settings?.active_profile || 'Custom' }}</strong>
                </div>
                <div class="osd-row" title="Field of View of your selected Telescope + Camera sensor (green frame)">
                    <span class="osd-dot optics"></span>
                    <span class="osd-lbl">Selected Optics:</span>
                    <strong class="optics-val">{{ systemSensorFov.w.toFixed(2) }}° × {{ systemSensorFov.h.toFixed(2) }}°</strong>
                </div>
                <div class="osd-row" title="Field of View of the currently displayed sky survey image">
                    <span class="osd-dot view"></span>
                    <span class="osd-lbl">Current View:</span>
                    <strong class="view-val">{{ (object.image_fov || localFov).toFixed(2) }}° × {{ (object.image_fov || localFov).toFixed(2) }}°</strong>
                </div>
                <div v-if="isZoomModified" class="osd-row">
                    <span class="osd-lbl">Canvas Zoom:</span>
                    <strong>{{ zoomLevel.toFixed(1) }}×</strong>
                </div>
            </div>

            <!-- Bottom Floating Canvas HUD Toolbar -->
            <div class="canvas-hud">
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
                        @keyup.enter="updateImage"
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
                        @click="updateImage"
                        title="Download new sky survey centered on current sensor rectangle and FOV"
                    >
                        {{ isObjectDownloading ? 'Fetching...' : (isModified ? '↻ Re-center & Fetch' : '↻ Refresh Image') }}
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
    width: 100%;
    height: 100%;
    display: flex;
    justify-content: center;
    align-items: center;
}

.sky-image {
    width: 100%;
    height: 100%;
    object-fit: contain;
    user-select: none;
    -webkit-user-drag: none;
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

/* Bottom Floating HUD Toolbar */
.canvas-hud {
    position: absolute;
    bottom: 16px;
    left: 50%;
    transform: translateX(-50%);
    background: rgba(17, 24, 39, 0.88);
    backdrop-filter: blur(8px);
    border: 1px solid #374151;
    border-radius: 10px;
    padding: 6px 12px;
    display: flex;
    align-items: center;
    gap: 10px;
    z-index: 100;
    box-shadow: 0 8px 20px rgba(0, 0, 0, 0.6);
    max-width: 95%;
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

.hud-btn.primary {
    border-color: #10b981;
    color: #10b981;
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

    .canvas-hud {
        bottom: 8px;
        padding: 6px 8px;
        gap: 6px 10px;
        width: calc(100% - 16px);
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
</style>
