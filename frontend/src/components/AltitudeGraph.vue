<script setup>
import { computed, ref } from 'vue';

const props = defineProps(['object', 'location', 'nightTimes']);

const showMoon = ref(true);

const width = 300;
const height = 150; // Increased height
const maxAlt = 90;

const getX = (timeStr) => {
    if (!props.object || !props.object.altitude_graph || props.object.altitude_graph.length === 0) return 0;

    const start = new Date(props.object.altitude_graph[0].time).getTime();
    const end = new Date(props.object.altitude_graph[props.object.altitude_graph.length - 1].time).getTime();
    const target = new Date(timeStr).getTime();

    const range = end - start;
    if (range <= 0) return 0;

    const pct = (target - start) / range;
    return pct * width;
};

// Generate Time Labels (Now, +6h, +12h, +18h)
const timeLabels = computed(() => {
    if (!props.object || !props.object.altitude_graph || props.object.altitude_graph.length === 0) return [];

    const start = new Date(props.object.altitude_graph[0].time);
    const labels = [];

    for (let i = 0; i <= 24; i += 6) {
        const t = new Date(start.getTime() + i * 60 * 60 * 1000);
        // Format: HH:MM
        const str = t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
        labels.push({ x: (i / 24) * width, text: str });
    }
    return labels;
});

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

    mapZone('civil', '#3b82f6', 0.2);
    mapZone('nautical', '#2563eb', 0.3);
    mapZone('astronomical', '#1d4ed8', 0.4);
    mapZone('night', '#1e3a8a', 0.5);
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
            <!-- Moved Legend Here -->
            <div class="legend-inline" v-if="object">
                 <span class="target-label">■ {{ object.name }}</span>
                 <span v-if="showMoon" class="moon-label">■ Moon</span>
                 <span class="night-label">■ Night</span>
            </div>
            <div class="controls">
                <input type="checkbox" id="chkMoon" v-model="showMoon">
                <label for="chkMoon">Moon</label>
            </div>
        </div>
        <div class="chart-container">
             <svg :viewBox="`0 0 ${width} ${height + 20}`" preserveAspectRatio="none" class="chart-svg">
                <g class="graph-area">
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

                    <!-- Target Graph -->
                    <path v-if="objectPath" :d="objectPath" fill="rgba(16, 185, 129, 0.4)" stroke="#10b981" stroke-width="2" />

                    <!-- Moon Graph -->
                    <path v-if="moonPath" :d="moonPath" fill="none" stroke="#fbbf24" stroke-width="2" stroke-dasharray="5,2" />
                </g>

                <!-- Labels -->
                <text x="5" :y="height*0.33 + 4" fill="#888" font-size="10">60°</text>
                <text x="5" :y="height*0.66 + 4" fill="#888" font-size="10">30°</text>

                <!-- X-Axis Labels -->
                <g transform="translate(0, 15)">
                    <text v-for="(lbl, i) in timeLabels" :key="i" :x="lbl.x" :y="height" fill="#aaa" font-size="10" text-anchor="middle">{{ lbl.text }}</text>
                </g>
             </svg>
        </div>
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
.legend-inline {
    display: flex;
    gap: 10px;
    font-size: 0.75rem;
}
.target-label { color: #10b981; }
.moon-label { color: #fbbf24; }
.night-label { color: #1e3a8a; font-weight: bold; }
</style>