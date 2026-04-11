<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue';

const props = defineProps(['object', 'objects', 'settings', 'clientSettings', 'searchQuery', 'activeFov']);
const emit = defineEmits(['update-settings', 'update-client-settings', 'update-search', 'select-object', 'update-fov']);

// --- LOCAL STATE ---
const localFov = ref(props.activeFov || 1.0);

watch(() => props.activeFov, (newVal) => {
    if (Math.abs(newVal - localFov.value) > 0.01) localFov.value = newVal;
});

const isObjectDownloading = computed(() => {
    return props.object && props.object.status === 'downloading';
});

// --- PHYSICAL SENSOR CALCULATION ---
// Calculates the sensor size in degrees based on HARDWARE settings.
const systemSensorFov = computed(() => {
    const tel = props.settings?.telescope;
    const cam = props.settings?.camera;
    if (!tel?.focal_length || !cam?.sensor_width || !cam?.sensor_height) {
        return { w: 0, h: 0 };
    }

    // (Sensor / FL) * 57.3
    const w = (cam.sensor_width / tel.focal_length) * 57.2958;
    const h = (cam.sensor_height / tel.focal_length) * 57.2958;
    return { w, h };
});

// --- SENSOR RECTANGLE STYLE ---
const sensorStyle = computed(() => {
    const sFov = systemSensorFov.value;

    // Denominator: The FOV of the background image.
    // If we have a downloaded image, use its metadata (which App.vue ensures is correct).
    // Fallback to localFov only if no image exists yet.
    const baseFov = props.object?.image_fov || localFov.value;

    if (sFov.w <= 0 || baseFov <= 0.001) return { display: 'none' };

    // Calculate percentage relative to the BACKGROUND IMAGE size
    const wPct = (sFov.w / baseFov) * 100;
    const hPct = (sFov.h / baseFov) * 100;

    return {
        width: `${wPct}%`,
        height: `${hPct}%`,
        position: 'absolute',
        top: `calc(50% + ${offsetY.value}px)`,
        left: `calc(50% + ${offsetX.value}px)`,
        transform: `translate(-50%, -50%) rotate(${rotation.value}deg)`,
        border: '2px solid red',
        boxShadow: '0 0 10px rgba(255,0,0,0.5)',
        cursor: 'move'
    };
});

// --- STANDARD LOGIC ---
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
const showSuggestions = ref(false);

const suggestionList = computed(() => {
    const q = (props.searchQuery || '').trim();
    if (!q) return [];
    const normQ = q.toLowerCase().replace(/\s+/g, '');
    const matches = (props.objects || []).filter(o => {
        if (o._normId === undefined) {
            o._normId = (o.name || '').toLowerCase().replace(/\s+/g, '');
            o._normCommon = (o.common_name || '').toLowerCase().replace(/\s+/g, '');
            o._normOther = (o.other_id || '').toLowerCase().replace(/\s+/g, '');
        }
        return o._normId.includes(normQ) || o._normCommon.includes(normQ);
    });
    return matches.slice(0, 10);
});

const onSearchInput = (e) => { emit('update-search', e.target.value); showSuggestions.value = true; };
const selectSuggestion = (obj) => { emit('update-search', obj.name); emit('select-object', obj); showSuggestions.value = false; };
const onSearchBlur = () => { setTimeout(() => { showSuggestions.value = false; }, 200); };

const resetFov = () => {
    if (systemSensorFov.value.w > 0) {
        localFov.value = parseFloat(systemSensorFov.value.w.toFixed(2));
        emit('update-fov', localFov.value);
    }
};

const viewportRef = ref(null);
const wrapperSize = ref(0);
let resizeObserver = null;

const zoomLevel = ref(1.0);

const onWheel = (e) => {
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
    const pxToDeg = baseFov / wrapperSize.value;
    
    const centerRa = parseFloat(props.object?.center_ra !== undefined ? props.object.center_ra : props.object.ra);
    const centerDec = parseFloat(props.object?.center_dec !== undefined ? props.object.center_dec : props.object.dec);
    
    const decOffset = - (offsetY.value * pxToDeg);
    
    // Protect against division by zero at poles
    const cosDec = Math.max(Math.cos(centerDec * Math.PI / 180), 0.01);
    const raOffset = - (offsetX.value * pxToDeg) / cosDec;
    
    return {
        ra: centerRa + raOffset,
        dec: centerDec + decOffset
    };
};

const isZoomModified = computed(() => zoomLevel.value !== 1.0);

const isFovModified = computed(() => {
    if (!props.object) return false;
    const padding = props.settings?.image_padding || 1.05;
    const currentTargetFov = localFov.value * padding;
    if (props.object.image_fov && Math.abs(currentTargetFov - props.object.image_fov) > 0.01) return true;
    return false;
});

const isModified = computed(() => {
    if (!props.object) return false;
    
    // Check if offsets have changed
    if (Math.abs(offsetX.value) > 0.01 || Math.abs(offsetY.value) > 0.01) return true;
    
    // Check if FOV has changed compared to the downloaded image
    const padding = props.settings?.image_padding || 1.05;
    const currentTargetFov = localFov.value * padding;
    if (props.object.image_fov && Math.abs(currentTargetFov - props.object.image_fov) > 0.01) return true;
    
    return false;
});

const updateImage = async () => {
    if (!props.object) return;
    const coords = getCurrentCenterCoordinates();
    
    props.object.status = 'downloading';
    
    const padding = props.settings?.image_padding || 1.05;
    const targetFov = localFov.value * padding;
    
    try {
        const res = await fetch('/api/fetch-custom-image', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                ra: coords.ra,
                dec: coords.dec,
                fov: targetFov,
                resolution: props.settings.image_server?.resolution || 512,
                source: props.settings.image_server?.source || "dss2r",
                timeout: props.settings.image_server?.timeout || 60
            })
        });
        
        if (res.ok) {
            const data = await res.json();
            
            // Add a timestamp cache-buster to force the browser to reload the image
            // Even though the URL changed, sometimes Vue caches the src attribute if the overall object structure didn't completely trigger a deep reactive update.
            const uniqueUrl = `${data.url}?t=${new Date().getTime()}`;
            
            props.object.image_url = uniqueUrl;
            props.object.center_ra = coords.ra;
            props.object.center_dec = coords.dec;
            props.object.image_fov = targetFov;
            props.object.status = 'cached';
            
            // Reset offsets since the image is now centered here
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
            for (let entry of entries) { wrapperSize.value = Math.min(entry.contentRect.width, entry.contentRect.height); }
        });
        resizeObserver.observe(viewportRef.value);
    }
});
onUnmounted(() => { if (resizeObserver) resizeObserver.disconnect(); });
</script>

<template>
    <article class="framing-panel">
        <header class="framing-header">
            <div class="header-left" style="flex-direction: column; align-items: flex-start; gap: 0;">
                <h2>{{ object.name }}</h2>
                <span class="subtitle">
                    <template v-if="object.common_name && object.common_name !== 'N/A'">
                        {{ object.common_name }} &bull; 
                    </template>
                    {{ object.constellation }}
                </span>
            </div>
            <div class="header-center">
                <div class="group">
                    <strong class="lbl">FOV</strong>
                    <div class="stepper-col">
                        <button class="stepper-btn" @click="localFov = parseFloat((localFov * 1.1).toFixed(2))">▲</button>
                        <button class="stepper-btn" @click="localFov = parseFloat((localFov * 0.9).toFixed(2))">▼</button>
                    </div>
                    <input type="number" v-model.number="localFov" step="0.01" class="input-mini" />
                </div>
                
                <div class="group" style="margin-left: 10px; padding-left: 10px; border-left: 1px solid #4b5563;">
                    <strong class="lbl">ROT</strong>
                    <div class="stepper-col">
                        <button class="stepper-btn" @mousedown="startRotate(1)" @mouseup="stopRotate" @mouseleave="stopRotate">▲</button>
                        <button class="stepper-btn" @mousedown="startRotate(-1)" @mouseup="stopRotate" @mouseleave="stopRotate">▼</button>
                    </div>
                    <input type="number" v-model.number="rotation" step="1" class="input-mini" />
                </div>
                
                <div class="group" style="margin-left: 10px; padding-left: 10px; border-left: 1px solid #4b5563;">
                    <div class="action-col">
                        <button class="outline mini primary-action full-w" 
                            @click="resetFov" 
                            :class="{ 'modified-warning': isFovModified }"
                            title="Reset to setup FOV">Reset FOV</button>
                        <button class="outline mini primary-action full-w" 
                            @click="resetZoom" 
                            :class="{ 'modified-warning': isZoomModified }"
                            title="Reset image zoom">Reset Zoom</button>
                    </div>
                </div>

                <div class="group" style="margin-left: 10px; padding-left: 10px; border-left: 1px solid #4b5563;">
                    <div class="action-col">
                        <button class="outline mini primary-action full-w" 
                            @click="updateImage" 
                            :disabled="isObjectDownloading" 
                            :class="{ 'modified-warning': isModified }"
                            title="Download new image with current center and FOV">
                            {{ isObjectDownloading ? '...' : 'Update/Center Image' }}
                        </button>
                        <button class="outline mini primary-action full-w" @click="sendToNina" title="Send framing to N.I.N.A (Requires Advanced API plugin installed in N.I.N.A)">
                            {{ ninaStatus || 'Send to NINA' }}
                        </button>
                    </div>
                </div>
            </div>
            <div class="header-right">
                <div class="search-container relative">
                    <input type="text" :value="searchQuery" @input="onSearchInput" placeholder="Search..."
                        @focus="showSuggestions = true" @blur="onSearchBlur" class="header-search" />
                    <div v-if="showSuggestions && suggestionList.length > 0" class="suggestions-popover">
                        <div v-for="s in suggestionList" :key="s.name" class="suggestion-item"
                            @click="selectSuggestion(s)">
                            {{ s.name }}
                        </div>
                    </div>
                </div>
            </div>
        </header>

        <div class="framing-viewport" ref="viewportRef" @mousemove="onDrag" @mouseup="stopDrag" @mouseleave="stopDrag" @wheel.prevent="onWheel">
            <div class="image-wrapper" :style="{ width: wrapperSize + 'px', height: wrapperSize + 'px', transform: `scale(${zoomLevel})` }">

                <div class="image-container">
                    <img v-if="object.image_url" :src="object.image_url" class="sky-image" />
                    <div v-else class="sky-image placeholder-text">
                        <span>No Image</span>
                    </div>
                </div>

                <div v-if="isObjectDownloading" class="download-overlay">
                    <div class="spinner"></div>
                    <span>Downloading new view...</span>
                </div>

                <div v-if="object && settings && !isObjectDownloading" :style="sensorStyle" @mousedown="startDrag">
                    <div class="crosshair">+</div>
                </div>
            </div>

            <div class="osd-overlay">
                <div>Object: {{ object.name }}</div>
                <div>Image FOV: {{ (object.image_fov || localFov).toFixed(1) }}°</div>
                <div>Sensor: {{ systemSensorFov.w.toFixed(1) }}° x {{ systemSensorFov.h.toFixed(1) }}°</div>
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
}

.framing-header {
    padding: 8px;
    background: #1f2937;
    border-bottom: 1px solid #374151;
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.header-left {
    display: flex;
    align-items: baseline;
    gap: 10px;
    min-width: 200px;
}

.header-left h2 {
    margin: 0;
    font-size: 1.5rem;
    color: #fff;
}

.subtitle {
    color: #9ca3af;
    font-size: 0.9rem;
}

.header-right {
    display: flex;
    justify-content: flex-end;
    min-width: 200px;
}

.header-center {
    display: flex;
    gap: 12px;
    align-items: center;
    justify-content: center;
    flex: 1;
}

.lbl {
    color: #9ca3af;
    font-size: 0.75rem;
    margin-right: 2px;
    text-transform: uppercase;
}

.input-mini {
    background: #000;
    color: white;
    border: 1px solid #4b5563;
    padding: 0 4px;
    font-size: 0.9rem;
    width: 65px;
    text-align: center;
    border-radius: 2px;
    height: 36px;
    box-sizing: border-box;
}

.stepper-col {
    display: flex;
    flex-direction: column;
    gap: 2px;
    margin-right: 4px;
}

.stepper-btn {
    background: #1f2937;
    color: #9ca3af;
    border: 1px solid #4b5563;
    border-radius: 2px;
    height: 17px;
    width: 20px;
    font-size: 0.6rem;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    padding: 0;
}

.stepper-btn:hover {
    background: #374151;
    color: white;
}

.action-col {
    display: flex;
    flex-direction: column;
    gap: 4px;
}

.full-w {
    width: 100%;
}

.mini {
    padding: 2px 8px;
    font-size: 0.8rem;
    height: 22px;
    line-height: 1;
    border: 1px solid #4b5563;
    background: transparent;
    color: #fff;
    cursor: pointer;
}

.mini:hover {
    background: #374151;
}

.primary-action {
    color: #10b981;
    border-color: #10b981;
    margin-left: 5px;
    font-weight: bold;
    transition: all 0.2s;
}

.modified-warning {
    color: #ef4444 !important;
    border-color: #ef4444 !important;
    background-color: rgba(239, 68, 68, 0.1) !important;
}

.modified-warning:hover {
    background-color: rgba(239, 68, 68, 0.2) !important;
}

.search-container {
    position: relative;
    width: 200px;
}

.header-search {
    width: 100%;
    padding: 4px 8px;
    background: #111827;
    border: 1px solid #4b5563;
    color: white;
    font-size: 0.9rem;
    border-radius: 4px;
}

.header-search:focus {
    outline: none;
    border-color: #3b82f6;
}

.suggestions-popover {
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    background: #1f2937;
    border: 1px solid #4b5563;
    max-height: 300px;
    overflow-y: auto;
    z-index: 50;
    box-shadow: 0 4px 6px rgba(0, 0, 0, 0.5);
}

.suggestion-item {
    padding: 6px 10px;
    cursor: pointer;
    font-size: 0.9rem;
    border-bottom: 1px solid #374151;
}

.suggestion-item:hover {
    background: #374151;
}

.group {
    display: flex;
    gap: 2px;
    align-items: center;
}

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
    font-size: 1.2rem;
    border: 1px dashed #374151;
}

.crosshair {
    position: absolute;
    left: 50%;
    top: 50%;
    transform: translate(-50%, -50%);
    color: red;
    pointer-events: none;
    font-size: 20px;
}

.osd-overlay {
    position: absolute;
    bottom: 0;
    right: 0;
    background: rgba(0, 0, 0, 0.8);
    color: lime;
    font-size: 11px;
    padding: 4px;
    pointer-events: none;
    z-index: 100;
    text-align: right;
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
    0% {
        transform: rotate(0deg);
    }

    100% {
        transform: rotate(360deg);
    }
}
</style>