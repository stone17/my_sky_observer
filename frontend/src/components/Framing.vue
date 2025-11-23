<script setup>
import { ref, computed, watch, inject } from 'vue';

const props = defineProps(['object', 'settings']); // Settings injected from App

const rotation = ref(0);
const offsetX = ref(0);
const offsetY = ref(0);
const isDragging = ref(false);
const dragStart = ref({ x: 0, y: 0 });
const customImageUrl = ref(null);
const isFetching = ref(false);

// Current FOV estimate
const currentFov = ref(1.0); // degrees

// Initialize currentFov from settings and object
watch(() => props.object, (newObj) => {
    if (newObj && props.settings && props.settings.telescope) {
        // Calculate sensor FOV width
        const fl = props.settings.telescope.focal_length;
        const sw = props.settings.camera.sensor_width;
        const sensorFovW = (sw / fl) * 57.2958;

        // Calculate download FOV based on rectangle percent
        if (newObj.fov_rectangle && newObj.fov_rectangle.width_percent) {
            currentFov.value = sensorFovW / (newObj.fov_rectangle.width_percent / 100);
        } else {
            currentFov.value = sensorFovW * 1.5; // Fallback
        }
    }
    customImageUrl.value = null;
    rotation.value = 0;
}, { immediate: true });

const fovStyle = computed(() => {
    if (!props.object || !props.settings) return {};

    // Recalculate percents based on currentFov vs Sensor FOV
    const fl = props.settings.telescope.focal_length;
    const sw = props.settings.camera.sensor_width;
    const sh = props.settings.camera.sensor_height;

    const sensorFovW = (sw / fl) * 57.2958;
    const sensorFovH = (sh / fl) * 57.2958;

    // If currentFov matches the image size
    const wPct = (sensorFovW / currentFov.value) * 100;
    const hPct = (sensorFovH / currentFov.value) * 100;

    return {
        width: `${wPct}%`,
        height: `${hPct}%`,
        top: `calc(50% - ${hPct/2}% + ${offsetY.value}px)`,
        left: `calc(50% - ${wPct/2}% + ${offsetX.value}px)`,
        transform: `rotate(${rotation.value}deg)`,
        position: 'absolute',
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
            customImageUrl.value = data.url;
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
</script>

<template>
  <article class="framing-panel">
    <header class="framing-header">
        <div class="controls-row">
             <div class="group">
                <strong>FOV (deg):</strong>
                <button class="outline small" @click="currentFov = (currentFov * 0.8).toFixed(2)">-</button>
                <input type="number" v-model.number="currentFov" step="0.1" style="width: 60px" />
                <button class="outline small" @click="currentFov = (currentFov * 1.2).toFixed(2)">+</button>
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
    
    <div class="framing-viewport" @mousemove="onDrag" @mouseup="stopDrag" @mouseleave="stopDrag">
        <div class="image-wrapper">
            <img :src="customImageUrl || object.image_url || 'https://via.placeholder.com/500?text=Loading...'" class="sky-image" />

            <!-- FOV Overlay -->
            <div
                v-if="object && settings"
                :style="fovStyle"
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
    display: flex;
    justify-content: center;
    align-items: center;
    overflow: hidden;
    position: relative;
}
.image-wrapper {
    position: relative;
    height: 100%;
    aspect-ratio: 1/1;
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
}
</style>