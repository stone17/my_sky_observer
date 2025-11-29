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

// Generate Time Ticks (Full hours and half hours)
const timeTicks = computed(() => {
    if (!props.object || !props.object.altitude_graph || props.object.altitude_graph.length === 0) return [];

    const start = new Date(props.object.altitude_graph[0].time);
    const end = new Date(props.object.altitude_graph[props.object.altitude_graph.length - 1].time);
    const startTime = start.getTime();
    const endTime = end.getTime();
    const range = endTime - startTime;

    if (range <= 0) return [];

    const ticks = [];
    
    // Start at the next half-hour boundary
    let current = new Date(startTime);
    if (current.getMinutes() > 30) {
        current.setHours(current.getHours() + 1, 0, 0, 0);
    } else if (current.getMinutes() > 0) {
        current.setMinutes(30, 0, 0);
    } else {
        current.setMinutes(0, 0, 0);
    }

    while (current.getTime() <= endTime) {
        const t = current.getTime();
        const pct = (t - startTime) / range;
        const x = pct * width;
        
        const isFullHour = current.getMinutes() === 0;
        const label = isFullHour ? current.getHours() : null; // Only show hour number

        ticks.push({ 
            x, 
            label, 
            isMajor: isFullHour 
        });

        // Increment by 30 mins
        current.setTime(current.getTime() + 30 * 60 * 1000);
    }
    return ticks;
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

                <!-- X-Axis Ticks & Labels -->
                <g>
                    <g v-for="(tick, i) in timeTicks" :key="i">
                        <!-- Tick Line -->
                        <line 
                            :x1="tick.x" 
                            :y1="height" 
                            :x2="tick.x" 
                            :y2="height + (tick.isMajor ? 5 : 3)" 
                            stroke="#666" 
                            :stroke-width="tick.isMajor ? 1.5 : 1" 
                        />
                        <!-- Label (Major only) -->
                        <text 
                            v-if="tick.label !== null" 
                            :x="tick.x" 
                            :y="height + 15" 
                            fill="#aaa" 
                            font-size="9" 
                            text-anchor="middle"
                        >{{ tick.label }}</text>
                    </g>
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