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
const emit = defineEmits(['update-settings', 'update-client-settings', 'update-search', 'select-object', 'update-fov']);

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
        cursor: 'move'
    };
});

// --- ROTATION & DRAG LOGIC ---
const rotation = ref(0);
let rotInterval = null;
let rotTimeout = null;

const startRotate = (dir) => {
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
};

const offsetX = ref(0);
const offsetY = ref(0);
const isDragging = ref(false);
const dragStart = ref({ x: 0, y: 0 });

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
const stopDrag = () => { isDragging.value = false; };

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
                    <span>{{ ninaStatus || 'Send to N.I.N.A.' }}</span>
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

                <div v-if="isObjectDownloading" class="download-overlay">
                    <div class="spinner"></div>
                    <span>Downloading sky survey...</span>
                </div>

                <div
                    v-if="object && settings && !isObjectDownloading"
                    :style="sensorStyle"
                    @mousedown="startDrag"
                    title="Drag to re-center framing (Shift + Scroll to rotate)"
                >
                    <div class="crosshair">+</div>
                </div>
            </div>

            <!-- Top-Right Telemetry Readout -->
            <div class="osd-overlay">
                <div>Image FOV: <strong>{{ (object.image_fov || localFov).toFixed(2) }}°</strong></div>
                <div>Sensor: <strong>{{ systemSensorFov.w.toFixed(2) }}° × {{ systemSensorFov.h.toFixed(2) }}°</strong></div>
                <div v-if="isZoomModified">Zoom: <strong>{{ zoomLevel.toFixed(1) }}×</strong></div>
            </div>

            <!-- Bottom Floating Canvas HUD Toolbar -->
            <div class="canvas-hud">
                <!-- FOV Control -->
                <div class="hud-group">
                    <span class="hud-label">FOV (°)</span>
                    <button
                        type="button"
                        class="hud-step-btn"
                        @click="stepFov(0.9)"
                        title="Decrease FOV (Zoom In)"
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
                        title="Increase FOV (Zoom Out)"
                    >
                        +
                    </button>
                </div>

                <div class="hud-divider"></div>

                <!-- Rotation Control -->
                <div class="hud-group">
                    <span class="hud-label">ROT (°)</span>
                    <button
                        type="button"
                        class="hud-step-btn"
                        @mousedown="startRotate(-1)"
                        @mouseup="stopRotate"
                        @mouseleave="stopRotate"
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

/* Top-Right Telemetry Overlay */
.osd-overlay {
    position: absolute;
    top: 12px;
    right: 12px;
    background: rgba(15, 23, 42, 0.8);
    backdrop-filter: blur(6px);
    border: 1px solid rgba(51, 65, 85, 0.8);
    border-radius: 6px;
    color: #94a3b8;
    font-size: 0.72rem;
    padding: 6px 10px;
    pointer-events: none;
    z-index: 90;
    text-align: right;
    line-height: 1.4;
}

.osd-overlay strong {
    color: #10b981;
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

@media (max-width: 768px) {
    .framing-header {
        padding: 8px 10px;
    }
    .canvas-hud {
        bottom: 10px;
        padding: 6px 8px;
        gap: 6px;
    }
    .hud-divider {
        display: none;
    }
}
</style>
