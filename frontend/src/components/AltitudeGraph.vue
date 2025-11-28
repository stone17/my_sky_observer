<script setup>
import { computed, ref } from 'vue';

const props = defineProps(['object', 'location', 'nightTimes']);

const showMoon = ref(true);

const width = 300;
const height = 150; // Increased height
const maxAlt = 90;

const getX = (timeStr) => {
    // Map time string (ISO) to X coordinate (0-300)
    // We assume the graph covers 24 hours starting from "now"
    // But wait, the backend points are generated from "now" for +24h.
    // Ideally we should use the index if points are evenly spaced, which they are.
    // However, night times are absolute ISO strings.
    // So we need to calculate offset relative to the start of the graph.

    if (!props.object || !props.object.altitude_graph || props.object.altitude_graph.length === 0) return 0;

    const start = new Date(props.object.altitude_graph[0].time).getTime();
    const end = new Date(props.object.altitude_graph[props.object.altitude_graph.length - 1].time).getTime();
    const target = new Date(timeStr).getTime();

    const range = end - start;
    if (range <= 0) return 0;

    const pct = (target - start) / range;
    return pct * width;
};

const getPath = (data, isMoon = false) => {
    if (!data || data.length === 0) return "";

    const stepX = width / (data.length - 1);
    let d = "";

    data.forEach((point, i) => {
        const x = i * stepX;
        const y = height - (Math.max(0, point.altitude) / maxAlt) * height;
        d += (i === 0 ? "M" : "L") + ` ${x} ${y}`;
    });

    if (!isMoon) {
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

const nightZones = computed(() => {
    if (!props.nightTimes) return [];

    // Structure: { type: 'astro', x1: 10, x2: 50, color: '...' }
    // nightTimes keys: night, astronomical, nautical, civil (evening), etc.
    // But they are intervals [start, end].
    // We want to draw rectangles.

    const zones = [];

    const mapZone = (key, color, opacity) => {
        if (props.nightTimes[key] && props.nightTimes[key].length === 2) {
            const x1 = Math.max(0, getX(props.nightTimes[key][0]));
            const x2 = Math.min(width, getX(props.nightTimes[key][1]));

            if (x2 > x1) {
                zones.push({ x: x1, width: x2 - x1, color, opacity });
            }
        }
    };

    // Order matters (draw darker on top of lighter? or disjoint?)
    // The backend returns overlapping periods?
    // astro, nautical, civil define bands.
    // "night" is astro dark.

    // Let's just draw specific blocks provided by backend.
    // The backend provides: civil, nautical, astronomical (evening), night (full dark), astronomical_morn, etc.

    // Civil Twilight (Evening)
    mapZone('civil', '#3b82f6', 0.2);
    // Nautical Twilight (Evening)
    mapZone('nautical', '#2563eb', 0.3);
    // Astronomical Twilight (Evening)
    mapZone('astronomical', '#1d4ed8', 0.4);

    // Full Night
    mapZone('night', '#1e3a8a', 0.5);

    // Morning Reverse
    mapZone('astronomical_morn', '#1d4ed8', 0.4);
    mapZone('nautical_morn', '#2563eb', 0.3);
    mapZone('civil_morn', '#3b82f6', 0.2);

    return zones;
});

</script>

<template>
    <div class="altitude-wrapper">
        <div class="chart-header">
            <span class="title">Altitude & Visibility</span>
            <div class="controls">
                <input type="checkbox" id="chkMoon" v-model="showMoon">
                <label for="chkMoon">Moon</label>
            </div>
        </div>
        <div class="chart-container">
             <svg :viewBox="`0 0 ${width} ${height}`" preserveAspectRatio="none" class="chart-svg">
                <!-- Night Zones -->
                <rect
                    v-for="(z, i) in nightZones"
                    :key="i"
                    :x="z.x"
                    y="0"
                    :width="z.width"
                    :height="height"
                    :fill="z.color"
                    :fill-opacity="z.opacity"
                />

                <!-- Grid Lines -->
                <line x1="0" :y1="height*0.33" :x2="width" :y2="height*0.33" stroke="#444" stroke-dasharray="4" />
                <line x1="0" :y1="height*0.66" :x2="width" :y2="height*0.66" stroke="#444" stroke-dasharray="4" />

                <!-- Horizon -->
                <line x1="0" :y1="height" :x2="width" :y2="height" stroke="#666" stroke-width="2" />

                <!-- Target Graph (Fill + Stroke) -->
                <path v-if="objectPath" :d="objectPath" fill="rgba(16, 185, 129, 0.4)" stroke="#10b981" stroke-width="2" />

                <!-- Moon Graph (Stroke only) -->
                <path v-if="moonPath" :d="moonPath" fill="none" stroke="#fbbf24" stroke-width="2" stroke-dasharray="5,2" />

                <!-- Labels -->
                <text x="5" :y="height*0.33 + 4" fill="#888" font-size="10">60°</text>
                <text x="5" :y="height*0.66 + 4" fill="#888" font-size="10">30°</text>
             </svg>
        </div>
        <div class="legend" v-if="object">
             <span class="target-label">{{ object.name }}</span>
             <span v-if="showMoon" class="moon-label">Moon</span>
             <span class="night-label">Night</span>
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
.moon-label { color: #fbbf24; }
.night-label { color: #1e3a8a; font-weight: bold; }
</style>