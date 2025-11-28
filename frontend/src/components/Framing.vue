<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue';

const props = defineProps(['object', 'settings']);

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
    if (props.object && props.object.fov_rectangle && sensorFov.value.w > 0) {
        // object.fov_rectangle.width_percent = (sensorFovW / imageFov) * 100
        // So imageFov = sensorFovW / (width_percent / 100)
        const wPct = props.object.fov_rectangle.width_percent;
        if (wPct > 0) {
            const calculatedImageFov = sensorFov.value.w / (wPct / 100);
            imageFov.value = calculatedImageFov;
            currentFov.value = calculatedImageFov; // Start matched
        }
    }
};

watch(() => props.object, () => {
    customImageUrl.value = null;
    offsetX.value = 0;
    offsetY.value = 0;
    initFov();
}, { immediate: true });

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
        <div class="controls-row">
             <div class="group">
                <strong>FOV (deg):</strong>
                <button class="outline small" @click="currentFov = (currentFov * 1.2).toFixed(2)">-</button>
                <input type="number" v-model.number="currentFov" step="0.1" style="width: 60px" />
                <button class="outline small" @click="currentFov = (currentFov * 0.8).toFixed(2)">+</button>
                <button @click="fetchCustomFov" :disabled="isFetching">{{ isFetching ? '...' : 'Fetch' }}</button>
             </div>
             <div class="group">
                <strong>Rot:</strong>
                <button class="outline small" @click="rotation -= 5">↺</button>
                <input type="number" v-model.number="rotation" style="width: 50px" />
                <button class="outline small" @click="rotation += 5">↻</button>
             </div>
             <button class="primary" @click="sendToNina">To N.I.N.A</button>
        </div>
    </header>
    
    <div class="framing-viewport" ref="viewportRef" @mousemove="onDrag" @mouseup="stopDrag" @mouseleave="stopDrag">
        <!-- Image Layer -->
        <div class="image-wrapper" :style="{ width: wrapperSize + 'px', height: wrapperSize + 'px' }">
             <div class="image-container" :style="imageStyle">
                  <img :src="customImageUrl || object.image_url || 'https://via.placeholder.com/500?text=Loading...'" class="sky-image" />
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
    padding: 10px;
    background: #1f2937;
    border-bottom: 1px solid #374151;
}
.controls-row {
    display: flex;
    gap: 15px;
    align-items: center;
    justify-content: center;
    flex-wrap: wrap;
}
.group {
    display: flex;
    gap: 5px;
    align-items: center;
}
.small {
    padding: 2px 8px;
    font-size: 0.9rem;
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