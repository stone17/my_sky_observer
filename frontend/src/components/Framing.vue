<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue';

const props = defineProps(['object', 'objects', 'settings', 'clientSettings', 'searchQuery', 'availableTypes']);
const emit = defineEmits(['update-settings', 'update-client-settings', 'update-search', 'select-object']);

const rotation = ref(0);
const offsetX = ref(0);
const offsetY = ref(0);
const isDragging = ref(false);
const dragStart = ref({ x: 0, y: 0 });
const customImageUrl = ref(null);
const isFetching = ref(false);
const showSuggestions = ref(false);

// Search Logic (Normalized)
const suggestionList = computed(() => {
    const q = (props.searchQuery || '').trim();
    if (!q) return [];
    const normQ = q.toLowerCase().replace(/\s+/g, '');

    const matches = (props.objects || []).filter(o => {
        const normId = o.name.toLowerCase().replace(/\s+/g, '');
        const normCommon = (o.common_name || '').toLowerCase().replace(/\s+/g, '');
        const normOther = (o.other_id || '').toLowerCase().replace(/\s+/g, '');
        return normId.includes(normQ) || normCommon.includes(normQ) || normOther.includes(normQ);
    });

    matches.sort((a, b) => {
        const normA = a.name.toLowerCase().replace(/\s+/g, '');
        const normB = b.name.toLowerCase().replace(/\s+/g, '');
        if (normA === normQ && normB !== normQ) return -1;
        if (normB === normQ && normA !== normQ) return 1;
        const aStarts = normA.startsWith(normQ);
        const bStarts = normB.startsWith(normQ);
        if (aStarts && !bStarts) return -1;
        if (bStarts && !aStarts) return 1;
        if (normA.length !== normB.length) return normA.length - normB.length;
        return normA.localeCompare(normB);
    });
    return matches.slice(0, 10);
});

const onSearchInput = (e) => { emit('update-search', e.target.value); showSuggestions.value = true; };
const selectSuggestion = (obj) => { emit('update-search', obj.name); emit('select-object', obj); showSuggestions.value = false; };
const onSearchBlur = () => { setTimeout(() => { showSuggestions.value = false; }, 200); };

// Resize
const viewportRef = ref(null);
const wrapperSize = ref(0);
let resizeObserver = null;

// FOV
const currentFov = ref(0.1);
const imageFov = ref(0.1);
const sensorFov = ref({ w: 0, h: 0 });

const initFov = () => {
    if (props.object) {
        if (props.object.image_fov) {
            const iFov = props.object.image_fov;
            imageFov.value = iFov;
            if (currentFov.value <= 0.1 || Math.abs(currentFov.value - iFov) > 5.0) {
                currentFov.value = parseFloat(iFov.toFixed(1));
            }
        }
        if (props.object.sensor_fov) {
            sensorFov.value = props.object.sensor_fov;
        }
    }
};

watch(() => props.object, initFov, { deep: true, immediate: true });

const imageStyle = computed(() => {
    if (currentFov.value <= 0.001 || imageFov.value <= 0.001) return { display: 'none' };
    const scale = imageFov.value / currentFov.value;
    return { width: `${scale * 100}%`, height: `${scale * 100}%`, position: 'absolute', top: '50%', left: '50%', transform: `translate(-50%, -50%)` };
});

const sensorStyle = computed(() => {
    if (currentFov.value <= 0.001 || sensorFov.value.w <= 0) return { display: 'none' };
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

const fetchCustomFov = async () => { /* Logic hidden for brevity */ };
const onDrag = (e) => { if (isDragging.value) { offsetX.value = e.clientX - dragStart.value.x; offsetY.value = e.clientY - dragStart.value.y; } };
const startDrag = (e) => { isDragging.value = true; dragStart.value = { x: e.clientX - offsetX.value, y: e.clientY - offsetY.value }; e.preventDefault(); };
const stopDrag = () => { isDragging.value = false; };
const sendToNina = async () => { /* Logic hidden for brevity */ };

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
            <div class="header-left">
                <h2>{{ object.name }}</h2>
                <span class="subtitle">{{ object.constellation }}</span>
            </div>
            <div class="header-center">
                <div class="group">
                    <strong class="lbl">FOV</strong>
                    <button class="outline mini"
                        @click="currentFov = parseFloat((currentFov * 1.1).toFixed(1))">+</button>
                    <input type="number" v-model.number="currentFov" step="0.1" class="input-mini" />
                    <button class="outline mini"
                        @click="currentFov = parseFloat((currentFov * 0.9).toFixed(1))">-</button>
                </div>
            </div>
            <div class="header-right">
                <div class="search-container relative">
                    <input type="text" :value="searchQuery" @input="onSearchInput" placeholder="Search..."
                        @focus="showSuggestions = true" @blur="onSearchBlur" class="header-search" />
                    <div v-if="showSuggestions && suggestionList.length > 0" class="suggestions-popover">
                        <div v-for="s in suggestionList" :key="s.name" class="suggestion-item"
                            @click="selectSuggestion(s)">
                            {{ s.name }} <span v-if="s.common_name && s.common_name !== 'N/A'"
                                style="color: #9ca3af; font-size: 0.8em;">- {{ s.common_name }}</span>
                        </div>
                    </div>
                </div>
            </div>
        </header>

        <div class="framing-viewport" ref="viewportRef" @mousemove="onDrag" @mouseup="stopDrag" @mouseleave="stopDrag">
            <div class="image-wrapper" :style="{ width: wrapperSize + 'px', height: wrapperSize + 'px' }">
                <div class="image-container" :style="imageStyle">
                    <img v-if="customImageUrl || object.image_url" :src="customImageUrl || object.image_url"
                        class="sky-image" />
                    <div v-else class="sky-image placeholder-text">
                        <span v-if="object.status === 'downloading'">Downloading...</span>
                        <span v-else>No Image</span>
                    </div>
                </div>
                <div v-if="object && settings" :style="sensorStyle" @mousedown="startDrag">
                    <div class="crosshair">+</div>
                </div>
            </div>

            <div
                style="position: absolute; bottom: 0; left: 0; background: rgba(0,0,0,0.8); color: lime; font-size: 11px; padding: 4px; pointer-events: none; z-index: 100; text-align: left;">
                <div>Object: {{ object.name }}</div>
                <div>Sensor FOV: {{ sensorFov.w.toFixed(3) }}° x {{ sensorFov.h.toFixed(3) }}°</div>
                <div>Settings FL: {{ settings?.telescope?.focal_length }}</div>
            </div>
        </div>
    </article>
</template>

<style scoped>
/* (Same styles as before) */
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
    width: 48px;
    text-align: center;
    border-radius: 2px;
    height: 26px;
    box-sizing: border-box;
}

.mini {
    padding: 2px 8px;
    font-size: 0.9rem;
    height: 26px;
    line-height: 1;
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