<script setup>
import { ref, computed } from 'vue';
import {
    formatLocalClock,
    formatDateInputYmd,
    formatObsDateBadge,
    resolveObservingEvening,
    parseObserverLocation,
    computeNightEphemerisForEvening,
    resolveSessionRangeForEvening,
    buildTwilightSliderStyle
} from '../utils/observingTime.js';

const props = defineProps({
    isLiveTime: {
        type: Boolean,
        default: true
    },
    liveNowMs: {
        type: Number,
        default: () => Date.now()
    },
    effectiveTimeMs: {
        type: Number,
        default: () => Date.now()
    },
    object: {
        type: Object,
        default: null
    },
    settings: {
        type: Object,
        default: null
    },
    nightTimes: {
        type: Object,
        default: null
    },
    variant: {
        type: String,
        default: 'sky' // 'sky' | 'framing'
    }
});

const emit = defineEmits(['update-time']);

const FRAMING_COLLAPSE_KEY = 'sky_observer_framing_date_console_collapsed_v1';
const readSavedCollapsed = () => {
    try {
        return window.localStorage?.getItem(FRAMING_COLLAPSE_KEY) === '1';
    } catch {
        return false;
    }
};
const isCollapsed = ref(readSavedCollapsed());
const toggleCollapsed = () => {
    isCollapsed.value = !isCollapsed.value;
    try {
        window.localStorage?.setItem(FRAMING_COLLAPSE_KEY, isCollapsed.value ? '1' : '0');
    } catch {
        // Ignore storage errors
    }
};

const dateInputRef = ref(null);

const observerLocation = computed(() => parseObserverLocation(props.settings));

const observingEvening = computed(() =>
    resolveObservingEvening(props.isLiveTime, props.liveNowMs, props.effectiveTimeMs)
);

const activeNightEphemeris = computed(() => {
    const loc = observerLocation.value;
    if (!loc) return null;
    const eve = observingEvening.value;
    return computeNightEphemerisForEvening(eve.year, eve.month0, eve.day, loc.lat, loc.lon);
});

const sessionRange = computed(() =>
    resolveSessionRangeForEvening({
        evening: observingEvening.value,
        observerLocation: observerLocation.value,
        object: props.object,
        nightTimes: props.nightTimes
    })
);

const sliderValueMs = computed(() => {
    const { startMs, endMs } = sessionRange.value;
    return Math.max(startMs, Math.min(endMs, props.effectiveTimeMs));
});

const sliderTrackStyle = computed(() =>
    buildTwilightSliderStyle({
        startMs: sessionRange.value.startMs,
        endMs: sessionRange.value.endMs,
        evening: observingEvening.value,
        nightTimes: props.nightTimes,
        ephem: activeNightEphemeris.value
    })
);

const formattedObsTime = computed(() => formatLocalClock(props.effectiveTimeMs));
const formattedObsDate = computed(() =>
    formatObsDateBadge(props.effectiveTimeMs, props.liveNowMs)
);
const dateInputValue = computed(() => formatDateInputYmd(props.effectiveTimeMs));

const dayOffsetFromToday = computed(() => {
    const todayNoon = new Date(props.liveNowMs);
    todayNoon.setHours(12, 0, 0, 0);
    const obsNoon = new Date(props.effectiveTimeMs);
    obsNoon.setHours(12, 0, 0, 0);
    return Math.round((obsNoon.getTime() - todayNoon.getTime()) / 86400000);
});

const dayOffsetText = computed(() => {
    const d = dayOffsetFromToday.value;
    if (d === 0) return '';
    return d > 0 ? `+${d}d` : `−${Math.abs(d)}d`;
});

const formattedSessionStart = computed(() => formatLocalClock(sessionRange.value.startMs));
const formattedSessionEnd = computed(() => formatLocalClock(sessionRange.value.endMs));

const emitScrubTime = (ms) => {
    if (!Number.isFinite(ms)) return;
    emit('update-time', { isLive: false, timeMs: ms });
};

const onTimeSliderInput = (e) => {
    const val = Number(e.target?.value);
    if (!Number.isFinite(val)) return;
    emitScrubTime(val);
};

const stepObsTime = (deltaMinutes) => {
    emitScrubTime(props.effectiveTimeMs + deltaMinutes * 60 * 1000);
};

const stepObsDays = (deltaDays) => {
    const dt = new Date(props.effectiveTimeMs);
    dt.setDate(dt.getDate() + deltaDays);
    emitScrubTime(dt.getTime());
};

const onDateInputChange = (e) => {
    const raw = e.target?.value;
    if (!raw || typeof raw !== 'string') return;
    const parts = raw.split('-').map(Number);
    if (parts.length !== 3 || parts.some((n) => !Number.isFinite(n))) return;
    const [year, month, day] = parts;

    const cur = new Date(props.effectiveTimeMs);
    const next = new Date(
        year,
        month - 1,
        day,
        cur.getHours(),
        cur.getMinutes(),
        cur.getSeconds(),
        0
    );
    if (!Number.isFinite(next.getTime())) return;
    emitScrubTime(next.getTime());
};

const openDatePicker = () => {
    const el = dateInputRef.value;
    if (!el) return;
    if (typeof el.showPicker === 'function') {
        try {
            el.showPicker();
            return;
        } catch {
            // Fall through to focus/click fallback
        }
    }
    el.focus();
    el.click();
};

const onDateWheel = (e) => {
    if (e.deltaY === 0) return;
    const step = e.shiftKey ? 7 : 1;
    stepObsDays(e.deltaY < 0 ? step : -step);
};

const jumpToTonightMid = () => {
    emitScrubTime(sessionRange.value.midMs);
};

const resetToLiveNow = () => {
    emit('update-time', { isLive: true, timeMs: Date.now() });
};
</script>

<template>
    <div
        class="obs-date-console"
        :class="[
            `is-${variant}`,
            { 'is-collapsed': variant === 'framing' && isCollapsed }
        ]"
        @mousedown.stop
        @touchstart.stop
        @wheel.stop
    >
        <!-- Optional Header Slot (used by Sky View for Sky Fixed / Horizon Fixed / Drag Sky) -->
        <div v-if="$slots.header" class="console-header-slot">
            <slot name="header" />
        </div>

        <!-- Collapsed Single-Line Pill (Framing mode only when minimized) -->
        <div v-if="variant === 'framing' && isCollapsed" class="framing-collapsed-row">
            <button
                type="button"
                class="time-mini-btn"
                @click="stepObsDays(-1)"
                title="Previous day"
            >
                −1d
            </button>

            <div class="date-picker-wrap compact" @wheel.prevent="onDateWheel">
                <button
                    type="button"
                    class="date-picker-pill"
                    :class="{ 'is-offset': dayOffsetFromToday !== 0 }"
                    @click="openDatePicker"
                    title="Click to pick calendar date · Scroll wheel to cycle days (Shift+Wheel ±7d)"
                >
                    <span class="date-cal-icon">📅</span>
                    <span class="date-label-text">{{ formattedObsDate }}</span>
                    <span v-if="dayOffsetText" class="date-offset-tag">{{ dayOffsetText }}</span>
                </button>
                <input
                    ref="dateInputRef"
                    type="date"
                    class="native-date-input"
                    :value="dateInputValue"
                    tabindex="-1"
                    aria-label="Select observation date"
                    @input="onDateInputChange"
                />
            </div>

            <button
                type="button"
                class="time-mini-btn"
                @click="stepObsDays(1)"
                title="Next day"
            >
                +1d
            </button>

            <span class="inline-divider"></span>

            <div class="time-readout-group">
                <span class="time-mode-dot" :class="{ live: isLiveTime }"></span>
                <span class="time-clock-text">{{ formattedObsTime }}</span>
            </div>

            <button
                type="button"
                class="time-mini-btn now-btn"
                :class="{ active: isLiveTime }"
                @click="resetToLiveNow"
                title="Reset date & clock to live current time"
            >
                Now
            </button>

            <button
                type="button"
                class="collapse-chevron-btn"
                @click="toggleCollapsed"
                title="Expand observing date & night session slider"
                aria-label="Expand date console"
            >
                ▴
            </button>
        </div>

        <!-- Full Console Body -->
        <template v-else>
            <div class="console-controls-grid">
                <div class="time-date-row">
                    <div class="date-step-group">
                        <button
                            type="button"
                            class="time-mini-btn hide-xs"
                            @click="stepObsDays(-7)"
                            title="Step back 7 days (keep clock time)"
                        >
                            −7d
                        </button>
                        <button
                            type="button"
                            class="time-mini-btn"
                            @click="stepObsDays(-1)"
                            title="Previous day (keep clock time)"
                        >
                            −1d
                        </button>
                    </div>

                    <div class="date-picker-wrap" @wheel.prevent="onDateWheel">
                        <button
                            type="button"
                            class="date-picker-pill"
                            :class="{ 'is-offset': dayOffsetFromToday !== 0 }"
                            @click="openDatePicker"
                            title="Click to pick calendar date · Scroll wheel to cycle days (Shift+Wheel ±7d)"
                        >
                            <span class="date-cal-icon">📅</span>
                            <span class="date-label-text">{{ formattedObsDate }}</span>
                            <span v-if="dayOffsetText" class="date-offset-tag">
                                {{ dayOffsetText }}
                            </span>
                        </button>
                        <input
                            ref="dateInputRef"
                            type="date"
                            class="native-date-input"
                            :value="dateInputValue"
                            tabindex="-1"
                            aria-label="Select observation date"
                            @input="onDateInputChange"
                        />
                    </div>

                    <div class="date-step-group">
                        <button
                            type="button"
                            class="time-mini-btn"
                            @click="stepObsDays(1)"
                            title="Next day (keep clock time)"
                        >
                            +1d
                        </button>
                        <button
                            type="button"
                            class="time-mini-btn hide-xs"
                            @click="stepObsDays(7)"
                            title="Step forward 7 days (keep clock time)"
                        >
                            +7d
                        </button>
                    </div>
                </div>

                <span v-if="variant === 'framing'" class="inline-divider hide-narrow"></span>

                <div class="time-bar-top">
                    <span
                        v-if="observerLocation"
                        class="loc-pill"
                        :title="observerLocation.coordsLabel"
                    >
                        📍 {{ observerLocation.label }}
                    </span>

                    <div class="time-readout-group">
                        <span class="time-mode-dot" :class="{ live: isLiveTime }"></span>
                        <span class="time-clock-text">{{ formattedObsTime }}</span>
                    </div>

                    <div class="time-quick-btns">
                        <button
                            type="button"
                            class="time-mini-btn"
                            @click="stepObsTime(-60)"
                            title="Step back 1 hour"
                        >
                            −1h
                        </button>
                        <button
                            type="button"
                            class="time-mini-btn"
                            @click="stepObsTime(60)"
                            title="Step forward 1 hour"
                        >
                            +1h
                        </button>
                        <button
                            type="button"
                            class="time-mini-btn"
                            @click="jumpToTonightMid"
                            :title="
                                dayOffsetFromToday === 0
                                    ? 'Jump to tonight\'s dark-sky midpoint'
                                    : 'Jump to selected night\'s dark-sky midpoint'
                            "
                        >
                            {{ dayOffsetFromToday === 0 ? 'Tonight' : 'Mid' }}
                        </button>
                        <button
                            type="button"
                            class="time-mini-btn now-btn"
                            :class="{ active: isLiveTime }"
                            @click="resetToLiveNow"
                            title="Reset date & clock to live current time"
                        >
                            Now
                        </button>
                        <button
                            v-if="variant === 'framing'"
                            type="button"
                            class="collapse-chevron-btn"
                            @click="toggleCollapsed"
                            title="Minimize date console"
                            aria-label="Minimize date console"
                        >
                            ▾
                        </button>
                    </div>
                </div>
            </div>

            <div class="time-slider-row">
                <span class="session-bound">{{ formattedSessionStart }}</span>
                <input
                    type="range"
                    class="sky-time-slider"
                    :min="sessionRange.startMs"
                    :max="sessionRange.endMs"
                    :step="60000"
                    :value="sliderValueMs"
                    :style="sliderTrackStyle"
                    @input="onTimeSliderInput"
                    title="Scrub through selected night's observing session (Sunset → Astronomical Night → Sunrise)"
                />
                <span class="session-bound">{{ formattedSessionEnd }}</span>
            </div>
        </template>
    </div>
</template>

<style scoped>
.obs-date-console {
    display: flex;
    flex-direction: column;
    gap: 5px;
    padding: 6px 10px;
    border-radius: 8px;
    background: rgba(9, 14, 23, 0.94);
    border: 1px solid rgba(148, 163, 184, 0.26);
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.5);
    user-select: none;
    box-sizing: border-box;
}

/* Sky View bottom-right placement (zero backdrop-filter over 60fps canvas) */
.obs-date-console.is-sky {
    min-width: 320px;
    max-width: 396px;
}

/* Framing View compact horizontal bar above .canvas-hud */
.obs-date-console.is-framing {
    background: rgba(15, 22, 36, 0.9);
    border: 1px solid #374151;
    border-radius: 9px;
    padding: 5px 10px;
    gap: 4px;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
    max-width: 100%;
}

.obs-date-console.is-framing.is-collapsed {
    padding: 4px 8px;
}

.console-header-slot {
    padding-bottom: 4px;
    border-bottom: 1px solid rgba(148, 163, 184, 0.14);
}

.console-controls-grid {
    display: flex;
    flex-direction: column;
    gap: 5px;
}

.obs-date-console.is-framing .console-controls-grid {
    flex-direction: row;
    align-items: center;
    justify-content: center;
    flex-wrap: wrap;
    gap: 5px 8px;
}

.framing-collapsed-row {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    flex-wrap: nowrap;
}

.inline-divider {
    width: 1px;
    height: 16px;
    background: rgba(148, 163, 184, 0.22);
    flex-shrink: 0;
}

.time-date-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 5px;
    padding-bottom: 4px;
    border-bottom: 1px solid rgba(148, 163, 184, 0.14);
}

.obs-date-console.is-framing .time-date-row {
    padding-bottom: 0;
    border-bottom: none;
    justify-content: center;
}

.date-step-group {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    flex-shrink: 0;
}

.date-picker-wrap {
    position: relative;
    flex: 1;
    display: flex;
    justify-content: center;
    min-width: 118px;
}

.date-picker-wrap.compact {
    flex: 0 1 auto;
    min-width: 0;
}

.date-picker-pill {
    width: 100%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 5px;
    padding: 2px 8px;
    margin: 0;
    border-radius: 5px;
    border: 1px solid rgba(148, 163, 184, 0.24);
    background: rgba(15, 23, 38, 0.92);
    color: #e2e8f0;
    font-size: 0.72rem;
    font-weight: 500;
    line-height: 1.25;
    cursor: pointer;
    white-space: nowrap;
    transition: background-color 0.12s ease, border-color 0.12s ease, color 0.12s ease;
}

.date-picker-pill:hover {
    background: rgba(26, 38, 58, 0.96);
    border-color: rgba(56, 189, 248, 0.48);
    color: #ffffff;
}

.date-picker-pill.is-offset {
    border-color: rgba(245, 158, 11, 0.48);
    background: rgba(34, 26, 14, 0.88);
    color: #fef3c7;
}

.date-picker-pill.is-offset:hover {
    border-color: rgba(251, 191, 36, 0.72);
    background: rgba(46, 34, 16, 0.95);
}

.date-cal-icon {
    font-size: 0.72rem;
    line-height: 1;
}

.date-label-text {
    overflow: hidden;
    text-overflow: ellipsis;
}

.date-offset-tag {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.64rem;
    font-weight: 600;
    padding: 0 4px;
    border-radius: 3px;
    background: rgba(245, 158, 11, 0.2);
    color: #fcd34d;
}

.native-date-input {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    opacity: 0;
    pointer-events: none;
    border: 0;
    padding: 0;
    margin: 0;
}

.time-bar-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
}

.obs-date-console.is-framing .time-bar-top {
    justify-content: center;
    gap: 7px;
}

.loc-pill {
    font-size: 0.71rem;
    color: #94a3b8;
    max-width: 112px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.time-readout-group {
    display: inline-flex;
    align-items: center;
    gap: 5px;
}

.time-mode-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #f59e0b;
}

.time-mode-dot.live {
    background: #34d399;
}

.time-clock-text {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.78rem;
    font-weight: 600;
    color: #f1f5f9;
}

.time-quick-btns {
    display: inline-flex;
    align-items: center;
    gap: 3px;
}

.time-mini-btn {
    padding: 2px 6px;
    margin: 0;
    border-radius: 4px;
    border: 1px solid rgba(148, 163, 184, 0.24);
    background: rgba(18, 26, 40, 0.92);
    color: #cbd5e1;
    font-size: 0.68rem;
    font-weight: 500;
    line-height: 1.25;
    cursor: pointer;
    transition: background-color 0.12s ease, border-color 0.12s ease, color 0.12s ease;
}

.time-mini-btn:hover {
    background: rgba(30, 41, 59, 0.96);
    border-color: rgba(148, 163, 184, 0.48);
    color: #ffffff;
}

.time-mini-btn.now-btn.active {
    background: rgba(10, 46, 38, 0.95);
    border-color: rgba(52, 211, 153, 0.58);
    color: #a7f3d0;
}

.collapse-chevron-btn {
    padding: 2px 5px;
    margin: 0 0 0 1px;
    border-radius: 4px;
    border: 1px solid rgba(148, 163, 184, 0.2);
    background: rgba(15, 23, 38, 0.78);
    color: #94a3b8;
    font-size: 0.68rem;
    line-height: 1.25;
    cursor: pointer;
    transition: background-color 0.12s ease, color 0.12s ease;
}

.collapse-chevron-btn:hover {
    background: rgba(30, 41, 59, 0.96);
    color: #f8fafc;
}

.time-slider-row {
    display: flex;
    align-items: center;
    gap: 6px;
}

.session-bound {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.65rem;
    color: #64748b;
    flex-shrink: 0;
}

.sky-time-slider {
    flex: 1;
    height: 6px;
    margin: 0;
    padding: 0;
    border-radius: 999px;
    border: 1px solid rgba(148, 163, 184, 0.24);
    appearance: none;
    -webkit-appearance: none;
    cursor: pointer;
    outline: none;
}

.sky-time-slider::-webkit-slider-thumb {
    -webkit-appearance: none;
    appearance: none;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: #34d399;
    border: 1.5px solid #061018;
    cursor: pointer;
}

.sky-time-slider::-moz-range-thumb {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: #34d399;
    border: 1.5px solid #061018;
    cursor: pointer;
}

@media (max-width: 960px) {
    .obs-date-console.is-sky {
        max-width: none;
    }
    .hide-narrow {
        display: none;
    }
}

@media (max-width: 768px) {
    .obs-date-console.is-sky {
        min-width: 0;
    }
    .obs-date-console.is-framing .loc-pill,
    .hide-xs {
        display: none;
    }
}
</style>
