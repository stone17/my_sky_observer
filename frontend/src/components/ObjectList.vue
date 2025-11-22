<script setup>
import { computed } from 'vue';

const props = defineProps(['objects', 'selectedId']);
defineEmits(['select']);

// Helper to generate SVG path for altitude
const getAltitudePath = (altitudeGraph) => {
    if (!altitudeGraph || altitudeGraph.length === 0) return "";

    // Canvas size: 100x30
    const width = 100;
    const height = 30;
    const maxAlt = 90;

    // Normalize x (time) and y (altitude)
    // Time is assumed to be evenly spaced over 24h, so index maps to X
    const stepX = width / (altitudeGraph.length - 1);

    let d = `M 0 ${height}`; // Start bottom-left

    altitudeGraph.forEach((point, index) => {
        const x = index * stepX;
        const alt = point.altitude;
        // Y needs to be inverted (0 is top)
        // Map 0-90 deg to height-0
        const y = height - (Math.max(0, alt) / maxAlt) * height;
        d += ` L ${x} ${y}`;
    });

    d += ` L ${width} ${height} Z`; // Close path to bottom-right
    return d;
};
</script>

<template>
  <div class="scrollable-list">
    <div 
        v-for="obj in objects" 
        :key="obj.name" 
        class="object-card" 
        :class="{ active: selectedId === obj.name }"
        @click="$emit('select', obj)"
    >
      <header>
        <h4>{{ obj.name }}
            <span :class="['status-badge', `status-${obj.status}`]">{{ obj.status }}</span>
        </h4>
      </header>
      <small>
        Cat: {{ obj.catalog }} | Mag: {{ obj.mag }} | Size: {{ obj.size }}
      </small>
      <br/>

      <!-- Altitude Graph -->
      <div class="altitude-chart" v-if="obj.altitude_graph && obj.altitude_graph.length">
          <svg viewBox="0 0 100 30" preserveAspectRatio="none" width="100%" height="30">
              <!-- Horizon Line -->
              <line x1="0" y1="20" x2="100" y2="20" stroke="#444" stroke-width="1" stroke-dasharray="2" />
              <!-- Graph -->
              <path :d="getAltitudePath(obj.altitude_graph)" fill="rgba(0, 255, 0, 0.2)" stroke="#0f0" stroke-width="1" />
          </svg>
      </div>

      <small v-if="obj.hours_above_min > 0">
        Visible for {{ obj.hours_above_min }}h
      </small>
    </div>
    <div v-if="objects.length === 0" style="text-align: center; color: gray;">
        No objects loaded.
    </div>
  </div>
</template>

<style scoped>
.altitude-chart {
    margin-top: 5px;
    margin-bottom: 5px;
    background: #111;
    border-radius: 3px;
    overflow: hidden;
}
</style>