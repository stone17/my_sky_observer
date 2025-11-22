<script setup>
import { ref, computed, watch } from 'vue';

const props = defineProps(['object']);

// Framing state
const rotation = ref(0);
const offsetX = ref(0);
const offsetY = ref(0);
const isDragging = ref(false);
const dragStart = ref({ x: 0, y: 0 });

// Watch object change to reset framing
watch(() => props.object, () => {
    rotation.value = 0;
    offsetX.value = 0;
    offsetY.value = 0;
});

const fovStyle = computed(() => {
    if (!props.object || !props.object.fov_rectangle) return {};
    const rect = props.object.fov_rectangle;
    return {
        width: `${rect.width_percent}%`,
        height: `${rect.height_percent}%`,
        top: `calc(50% - ${rect.height_percent/2}% + ${offsetY.value}px)`,
        left: `calc(50% - ${rect.width_percent/2}% + ${offsetX.value}px)`,
        transform: `rotate(${rotation.value}deg)`,
        position: 'absolute',
        border: '2px solid red',
        boxShadow: '0 0 10px rgba(255,0,0,0.5)',
        cursor: 'move'
    };
});

const startDrag = (e) => {
    isDragging.value = true;
    dragStart.value = { x: e.clientX - offsetX.value, y: e.clientY - offsetY.value };
    e.preventDefault(); // Prevent text selection
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
    try {
        // We need to calculate the new center coordinates based on offsetX/offsetY
        // This assumes a linear projection which is "okay" for small offsets, 
        // but strictly speaking we need the WCS (World Coordinate System) of the image.
        // Since we don't have WCS in the frontend easily, and we are just visually framing 
        // relative to the center... 
        // Actually, shifting the FOV box on the image means we want the telescope to point 
        // where the center of the box is.
        // The image center corresponds to props.object.ra/dec.
        // We need to know the pixel scale or degrees per pixel to convert px offset to degrees.
        
        // For now, we will just send the original coordinates + rotation.
        // Implementing true offset calculation requires knowing the image scale in the frontend.
        // The backend sent `fov_rectangle` percents relative to the download FOV.
        // We can assume the download FOV corresponds to the image size.
        // But simpler MVP: Send rotation.
        
        const payload = {
            ra: props.object.ra,
            dec: props.object.dec,
            rotation: rotation.value
        };
        
        const res = await fetch('/api/nina/framing', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        
        if (res.ok) {
            alert("Sent to N.I.N.A successfully!");
        } else {
            const err = await res.json();
            alert("Error sending to N.I.N.A: " + (err.detail || "Unknown error"));
        }
    } catch (e) {
        alert("Network error sending to N.I.N.A");
    }
};
</script>

<template>
  <article>
    <header>
        <strong>{{ object.name }}</strong>
        <div style="float: right">
            <button class="secondary outline" @click="rotation -= 5">↺</button>
            <input type="number" v-model.number="rotation" style="width: 60px; display: inline-block; margin: 0 5px" />
            <button class="secondary outline" @click="rotation += 5">↻</button>
            <button @click="sendToNina">Send to N.I.N.A</button>
        </div>
    </header>
    
    <div class="framing-container" @mousemove="onDrag" @mouseup="stopDrag" @mouseleave="stopDrag">
        <img :src="object.image_url || 'https://via.placeholder.com/500?text=Loading...'" class="framing-image" />
        
        <!-- FOV Overlay -->
        <div 
            v-if="object.fov_rectangle" 
            :style="fovStyle" 
            @mousedown="startDrag"
        >
            <!-- Crosshair -->
            <div style="position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); color: red;">+</div>
        </div>
    </div>
    
    <footer>
        <p>
            <small>
                Use the red box to frame your shot. Drag to move, use buttons to rotate.
                (Note: Position offset calculation is not fully implemented in this version, only Rotation is sent).
            </small>
        </p>
    </footer>
  </article>
</template>