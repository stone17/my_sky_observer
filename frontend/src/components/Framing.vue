<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue';

const props = defineProps(['object', 'settings', 'clientSettings', 'availableTypes']);
const emit = defineEmits(['update-settings', 'update-client-settings']);

const rotation = ref(0);
const offsetX = ref(0);
const offsetY = ref(0);
const isDragging = ref(false);
const dragStart = ref({ x: 0, y: 0 });
const customImageUrl = ref(null);
const isFetching = ref(false);

// ResizeObserver State
const viewportRef = ref(null);
const wrapperSize = ref(0);
let resizeObserver = null;

// FOV State
const currentFov = ref(2.0); // Viewport FOV in degrees
const imageFov = ref(2.0);   // FOV of the currently loaded image

// Calculate Sensor FOV in degrees
const sensorFov = computed(() => {
    if (!props.settings?.camera || !props.settings?.telescope) return { w: 0, h: 0 };
    const fl = props.settings.telescope.focal_length;
    const sw = props.settings.camera.sensor_width;
    const sh = props.settings.camera.sensor_height;
    
    // Use exact formula: 2 * atan(sensor / (2 * focal_length))
    const fovW = 2 * Math.atan(sw / (2 * fl)) * (180 / Math.PI);
    const fovH = 2 * Math.atan(sh / (2 * fl)) * (180 / Math.PI);
    
    return { w: fovW, h: fovH };
});

// Initialize FOVs from object data
const initFov = () => {
    if (props.object) {
        // PREFERRED: Use explicit image_fov if available (from new backend)
        if (props.object.image_fov) {
            imageFov.value = props.object.image_fov;
            currentFov.value = parseFloat(props.object.image_fov.toFixed(1));
        }
        // FALLBACK: Use legacy fov_rectangle if image_fov not present
        else if (props.object.fov_rectangle && sensorFov.value.w > 0) {
            const wPct = props.object.fov_rectangle.width_percent;
            if (wPct > 0) {
                const calculatedImageFov = sensorFov.value.w / (wPct / 100);
                imageFov.value = calculatedImageFov;
                currentFov.value = parseFloat(calculatedImageFov.toFixed(1));
            }
        }
    }
};

// Watch object and sensorFov to initialize
watch([() => props.object, () => props.object?.image_fov, sensorFov], () => {
    initFov();
}, { immediate: true });

// Watch object specifically for resets
watch(() => props.object, () => {
    customImageUrl.value = null;
    offsetX.value = 0;
    offsetY.value = 0;
});

// Computed Styles
const imageStyle = computed(() => {
    if (currentFov.value <= 0) return {};
    const scale = imageFov.value / currentFov.value;
    return {
        width: `${scale * 100}%`,
        height: `${scale * 100}%`,
        position: 'absolute',
        top: '50%',
        left: '50%',
        transform: `translate(-50%, -50%)`
    };
});

const sensorStyle = computed(() => {
    if (currentFov.value <= 0) return {};
    const wPct = (sensorFov.value.w / currentFov.value) * 100;
    const hPct = (sensorFov.value.h / currentFov.value) * 100;
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

const fetchCustomFov = async () => {
    if (!props.object) return;
    isFetching.value = true;
    try {
        const res = await fetch('/api/fetch-custom-image', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                ra: props.object.ra,
                dec: props.object.dec,
                fov: currentFov.value
            })
        });
        if (res.ok) {
            const data = await res.json();
            customImageUrl.value = data.url + '?t=' + Date.now();
            // Update imageFov to match the newly fetched FOV
            imageFov.value = currentFov.value;
        }
    } catch (e) {
        console.error(e);
        alert("Failed to fetch image");
    } finally {
        isFetching.value = false;
    }
};

const startDrag = (e) => {
    isDragging.value = true;
    dragStart.value = { x: e.clientX - offsetX.value, y: e.clientY - offsetY.value };
    e.preventDefault();
};

const onDrag = (e) => {
    if (!isDragging.value) return;
    offsetX.value = e.clientX - dragStart.value.x;
    offsetY.value = e.clientY - dragStart.value.y;
};

const stopDrag = () => {
    isDragging.value = false;
};

const sendToNina = async () => {
    const payload = {
        ra: props.object.ra,
        dec: props.object.dec,
        rotation: rotation.value
    };

    try {
        const res = await fetch('/api/nina/framing', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        if (res.ok) alert("Sent to N.I.N.A");
        else alert("Error sending to N.I.N.A");
    } catch (e) { alert("Network Error"); }
};

onMounted(() => {
    if (viewportRef.value) {
        resizeObserver = new ResizeObserver(entries => {
            for (let entry of entries) {
                const { width, height } = entry.contentRect;
                wrapperSize.value = Math.min(width, height);
            }
        });
        resizeObserver.observe(viewportRef.value);
    }
});

onUnmounted(() => {
    if (resizeObserver) resizeObserver.disconnect();
});
</script>

<template>
  <article class="framing-panel">
    <header class="framing-header">
        <div class="header-left">
            <h2>{{ object.name }}</h2>
            <span class="subtitle">{{ object.constellation }}</span>
        </div>
    </header>

    <div class="controls-bar">
         <!-- Left: Framing Controls -->
         <div class="controls-section left">
             <div class="group">
                <strong>FOV:</strong>
                <button class="outline small" @click="currentFov = parseFloat((currentFov * 1.1).toFixed(1))">+</button>
                <input type="number" v-model.number="currentFov" step="0.1" class="input-sm" style="width: 50px;" />
                <button class="outline small" @click="currentFov = parseFloat((currentFov * 0.9).toFixed(1))">-</button>
                <button @click="fetchCustomFov" :disabled="isFetching" class="small">{{ isFetching ? '...' : 'Get' }}</button>
             </div>
             <div class="group">
                <strong>Rot:</strong>
                <button class="outline small" @click="rotation -= 5">↺</button>
                <input type="number" v-model.number="rotation" class="input-sm" style="width: 50px;" />
                <button class="outline small" @click="rotation += 5">↻</button>
             </div>
             <button class="primary small" @click="sendToNina">N.I.N.A</button>
        </div>
    </div>
    
    <div class="framing-viewport" ref="viewportRef" @mousemove="onDrag" @mouseup="stopDrag" @mouseleave="stopDrag">
        <!-- Image Layer -->
        <div class="image-wrapper" :style="{ width: wrapperSize + 'px', height: wrapperSize + 'px' }">
             <div class="image-container" :style="imageStyle">
                  <img v-if="customImageUrl || object.image_url" :src="customImageUrl || object.image_url" class="sky-image" />
                  <div v-else class="sky-image placeholder-text">
                      <span v-if="object.status === 'downloading'">Downloading...</span>
                      <span v-else>No Image</span>
                  </div>
             </div>

             <!-- Sensor Layer -->
             <div
                 v-if="object && settings"
                 :style="sensorStyle"
                 @mousedown="startDrag"
             >
                 <div class="crosshair">+</div>
             </div>
        </div>
        <!-- Debug Info -->
        <div style="position: absolute; bottom: 0; left: 0; background: rgba(0,0,0,0.7); color: lime; font-size: 10px; padding: 2px; pointer-events: none;">
            cFov: {{ currentFov }} | iFov: {{ imageFov }} | sFov: {{ sensorFov.w.toFixed(2) }}x{{ sensorFov.h.toFixed(2) }} | Rect: {{ object?.fov_rectangle?.width_percent?.toFixed(1) }}%
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
.controls-bar {
    padding: 8px;
    background: #1f2937;
    border-bottom: 1px solid #374151;
}
.controls-section {
    display: flex;
    gap: 10px;
    align-items: center;
}
.group {
    display: flex;
    gap: 2px;
    align-items: center;
    font-size: 0.85rem;
}
.input-sm {
    background: #000;
    color: white;
    border: 1px solid #4b5563;
    padding: 2px 4px;
    font-size: 0.85rem;
    width: 40px;
    text-align: center;
}
.small {
    padding: 2px 6px;
    font-size: 0.8rem;
}
.framing-viewport {
    flex: 1;
    background: #000;
    position: relative;
    overflow: hidden;
    /* Center coordinate system */
    display: flex;
    justify-content: center;
    align-items: center;
}
.image-wrapper {
    position: relative;
    /* Size set by JS */
    /* Ensure it doesn't overflow */
    display: flex;
    justify-content: center;
    align-items: center;
    background: #000; /* Optional: ensures black bars are black */
}
.image-container {
    /* Centering handled by absolute positioning in computed style */
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
</style>