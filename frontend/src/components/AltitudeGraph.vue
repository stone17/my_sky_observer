<script setup>
import { computed, ref } from 'vue';

const props = defineProps(['object', 'location']);

const showMoon = ref(true);

const getPath = (data, isMoon = false) => {
    if (!data || data.length === 0) return "";
    const width = 300;
    const height = 100;
    const maxAlt = 90;

    const stepX = width / (data.length - 1);

    // Start at bottom left for fill
    let d = "";

    // If it's moon, we might just want a line, not a fill.
    // Let's do line first.

    data.forEach((point, i) => {
        const x = i * stepX;
        const y = height - (Math.max(0, point.altitude) / maxAlt) * height;
        d += (i === 0 ? "M" : "L") + ` ${x} ${y}`;
    });

    if (!isMoon) {
        // Close the path for fill
        d += ` L ${width} ${height} L 0 ${height} Z`;
    }

    return d;
};

const objectPath = computed(() => {
    if (!props.object || !props.object.altitude_graph) return "";
    return getPath(props.object.altitude_graph);
});

const moonPath = computed(() => {
    if (!showMoon.value || !props.object || !props.object.moon_graph) return "";
    return getPath(props.object.moon_graph, true);
});

</script>

<template>
    <div class="altitude-wrapper">
        <div class="chart-header">
            <span class="title">Altitude</span>
            <div class="controls">
                <input type="checkbox" id="chkMoon" v-model="showMoon">
                <label for="chkMoon">Moon</label>
            </div>
        </div>
        <div class="chart-container">
             <svg viewBox="0 0 300 100" preserveAspectRatio="none" class="chart-svg">
                <!-- Grid Lines -->
                <line x1="0" y1="33" x2="300" y2="33" stroke="#333" stroke-dasharray="4" />
                <line x1="0" y1="66" x2="300" y2="66" stroke="#333" stroke-dasharray="4" />

                <!-- Horizon -->
                <line x1="0" y1="100" x2="300" y2="100" stroke="#666" stroke-width="2" />

                <!-- Target Graph (Fill + Stroke) -->
                <path v-if="objectPath" :d="objectPath" fill="rgba(16, 185, 129, 0.2)" stroke="#10b981" stroke-width="2" />

                <!-- Moon Graph (Stroke only) -->
                <path v-if="moonPath" :d="moonPath" fill="none" stroke="#fcd34d" stroke-width="2" stroke-dasharray="5,2" />

                <!-- Labels -->
                <text x="5" y="33" fill="#666" font-size="10">60°</text>
                <text x="5" y="66" fill="#666" font-size="10">30°</text>
             </svg>
        </div>
        <div class="legend" v-if="object">
             <span class="target-label">{{ object.name }}</span>
             <span v-if="showMoon" class="moon-label">Moon</span>
        </div>
        <div v-else class="legend">Select an object</div>
    </div>
</template>

<style scoped>
.altitude-wrapper {
    background: #111827;
    border: 1px solid #374151;
    border-radius: 4px;
    padding: 10px;
    height: 100%;
    display: flex;
    flex-direction: column;
}
.chart-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 5px;
    font-size: 0.8rem;
    color: #9ca3af;
}
.controls {
    display: flex;
    align-items: center;
    gap: 5px;
}
.chart-container {
    flex: 1;
    position: relative;
    min-height: 80px;
}
.chart-svg {
    width: 100%;
    height: 100%;
}
.legend {
    margin-top: 5px;
    font-size: 0.8rem;
    color: #9ca3af;
    text-align: center;
    display: flex;
    justify-content: center;
    gap: 15px;
}
.target-label { color: #10b981; }
.moon-label { color: #fcd34d; }
</style>