// Shared reactive Sky View layer toggles, reference-frame mode, and pre-indexed
// JPL Horizons ephemeris trajectories with O(log N) 3D spherical interpolation.

import { ref, shallowRef, watch } from 'vue';
import { DEG2RAD, RAD2DEG, wrapRaDeg } from './observingTime.js';

const STORAGE_KEYS = {
    frameMode: 'sky_observer_sky_frame_mode_v1',
    legacyFrameMode: 'sky_observer_sky_frame_mode',
    targetPinned: 'sky_observer_sky_target_pinned_v1',
    showHorizon: 'sky_observer_show_horizon_v1',
    showMeridian: 'sky_observer_show_meridian_v1',
    showSolarSystem: 'sky_observer_show_solar_system_v1',
    showEphemeris: 'sky_observer_show_ephemeris_v1',
    showCachedPhotos: 'sky_observer_show_photos_v1',
    showConstellations: 'sky_observer_show_constellations_v1',
    showStarLabels: 'sky_observer_show_star_labels_v1',
    showDsos: 'sky_observer_show_dsos_v1',
    applyTargetFilters: 'sky_observer_sky_apply_filters_v1',
    showGrid: 'sky_observer_show_grid_v1'
};

const readBool = (key, fallback) => {
    try {
        const raw = window.localStorage?.getItem(key);
        if (raw === '1') return true;
        if (raw === '0') return false;
    } catch {
        // Ignore storage access errors
    }
    return fallback;
};

const writeBool = (key, val) => {
    try {
        window.localStorage?.setItem(key, val ? '1' : '0');
    } catch {
        // Ignore storage access errors
    }
};

const readFrameMode = () => {
    try {
        const v1 = window.localStorage?.getItem(STORAGE_KEYS.frameMode);
        if (v1 === 'horizon' || v1 === 'sky') return v1;
        const legacy = window.localStorage?.getItem(STORAGE_KEYS.legacyFrameMode);
        if (legacy === 'horizon' || legacy === 'sky') return legacy;
    } catch {
        // Ignore storage access errors
    }
    return 'sky';
};

const bindPersistedBool = (key, defaultVal) => {
    const state = ref(readBool(key, defaultVal));
    watch(state, (val) => writeBool(key, Boolean(val)));
    return state;
};

export const skyFrameMode = ref(readFrameMode());
watch(skyFrameMode, (mode) => {
    if (mode !== 'horizon' && mode !== 'sky') return;
    try {
        window.localStorage?.setItem(STORAGE_KEYS.frameMode, mode);
    } catch {
        // Ignore storage access errors
    }
});

export const skyTargetPinned = bindPersistedBool(STORAGE_KEYS.targetPinned, false);
export const showHorizon = bindPersistedBool(STORAGE_KEYS.showHorizon, true);
export const showMeridian = bindPersistedBool(STORAGE_KEYS.showMeridian, false);
export const showSolarSystem = bindPersistedBool(STORAGE_KEYS.showSolarSystem, true);
export const showEphemeris = bindPersistedBool(STORAGE_KEYS.showEphemeris, true);
export const showCachedPhotos = bindPersistedBool(STORAGE_KEYS.showCachedPhotos, true);
export const showConstellations = bindPersistedBool(STORAGE_KEYS.showConstellations, true);
export const showStarLabels = bindPersistedBool(STORAGE_KEYS.showStarLabels, true);
export const showDsos = bindPersistedBool(STORAGE_KEYS.showDsos, true);
export const applyTargetFilters = bindPersistedBool(STORAGE_KEYS.applyTargetFilters, false);
export const showGrid = bindPersistedBool(STORAGE_KEYS.showGrid, true);

export const enterFrameModeToken = ref(0);
export const requestEnterFrameMode = () => {
    enterFrameModeToken.value += 1;
};

// --- Shared Ephemeris Modal & Pre-Indexed Trajectory Store ---
export const isEphemerisModalOpen = ref(false);
export const openEphemerisModal = () => {
    isEphemerisModalOpen.value = true;
};
export const closeEphemerisModal = () => {
    isEphemerisModalOpen.value = false;
};

export const pendingSkyMapCenter = ref(null);
export const locateInSkyView = (ra, dec, label = '') => {
    if (!Number.isFinite(Number(ra)) || !Number.isFinite(Number(dec))) return;
    pendingSkyMapCenter.value = {
        ra: wrapRaDeg(Number(ra)),
        dec: Math.max(-89.99, Math.min(89.99, Number(dec))),
        label,
        token: Date.now() + Math.random()
    };
};

export const savedEphemerides = shallowRef([]);
export const ephemeridesVersion = ref(0);
let ephemeridesLoadedOnce = false;
let ephemeridesFetchPromise = null;

/**
 * Pre-parses timestamps and 3D equatorial unit vectors once on load/download
 * so 60 FPS rendering never parses ISO strings or recomputes sin/cos per vertex.
 */
export const prepareEphemerisRecord = (raw) => {
    if (!raw || typeof raw !== 'object') return null;
    const srcPts = Array.isArray(raw.points) ? raw.points : [];
    const pts = [];
    for (let i = 0; i < srcPts.length; i++) {
        const p = srcPts[i];
        const ra = Number(p.ra);
        const dec = Number(p.dec);
        const tMs = typeof p.tMs === 'number' ? p.tMs : Date.parse(p.time);
        if (!Number.isFinite(ra) || !Number.isFinite(dec) || !Number.isFinite(tMs)) continue;

        const aRad = ra * DEG2RAD;
        const dRad = dec * DEG2RAD;
        const cd = Math.cos(dRad);
        pts.push({
            time: p.time,
            display_time: p.display_time || '',
            tMs,
            ra: wrapRaDeg(ra),
            dec,
            x: cd * Math.cos(aRad),
            y: cd * Math.sin(aRad),
            z: Math.sin(dRad),
            mag: p.mag !== null && p.mag !== undefined && Number.isFinite(Number(p.mag)) ? Number(p.mag) : null,
            distance_au:
                p.distance_au !== null &&
                p.distance_au !== undefined &&
                Number.isFinite(Number(p.distance_au))
                    ? Number(p.distance_au)
                    : null
        });
    }
    pts.sort((a, b) => a.tMs - b.tMs);

    return {
        ...raw,
        visible: raw.visible !== false,
        color: typeof raw.color === 'string' && raw.color.trim() ? raw.color.trim() : '#10B981',
        preparedPoints: pts,
        startMs: pts.length > 0 ? pts[0].tMs : NaN,
        stopMs: pts.length > 0 ? pts[pts.length - 1].tMs : NaN
    };
};

// Reusable scratch object for zero-GC 60 FPS interpolation
const interpScratch = {
    ra: 0,
    dec: 0,
    x: 1,
    y: 0,
    z: 0,
    mag: null,
    distance_au: null,
    inRange: true
};

/**
 * O(log N) spherical-chord interpolation of ephemeris position at `targetTimeMs`.
 * Smoothly glides between sampled JPL Horizons steps instead of snapping discretely.
 */
export const interpolateEphemerisAtTime = (ephem, targetTimeMs, out = interpScratch) => {
    const pts = ephem?.preparedPoints;
    if (!pts || pts.length === 0) return null;

    const n = pts.length;
    if (n === 1 || targetTimeMs <= pts[0].tMs) {
        const p0 = pts[0];
        out.ra = p0.ra;
        out.dec = p0.dec;
        out.x = p0.x;
        out.y = p0.y;
        out.z = p0.z;
        out.mag = p0.mag;
        out.distance_au = p0.distance_au;
        out.inRange = n === 1 || targetTimeMs >= p0.tMs - 3600000;
        return out;
    }

    const pLast = pts[n - 1];
    if (targetTimeMs >= pLast.tMs) {
        out.ra = pLast.ra;
        out.dec = pLast.dec;
        out.x = pLast.x;
        out.y = pLast.y;
        out.z = pLast.z;
        out.mag = pLast.mag;
        out.distance_au = pLast.distance_au;
        out.inRange = targetTimeMs <= pLast.tMs + 3600000;
        return out;
    }

    // Binary search for interval [lo, lo + 1] containing targetTimeMs
    let lo = 0;
    let hi = n - 1;
    while (lo + 1 < hi) {
        const mid = (lo + hi) >> 1;
        if (pts[mid].tMs <= targetTimeMs) lo = mid;
        else hi = mid;
    }

    const a = pts[lo];
    const b = pts[lo + 1];
    const span = b.tMs - a.tMs;
    const t = span > 0 ? Math.max(0.0, Math.min(1.0, (targetTimeMs - a.tMs) / span)) : 0.0;

    const nx = a.x + (b.x - a.x) * t;
    const ny = a.y + (b.y - a.y) * t;
    const nz = a.z + (b.z - a.z) * t;
    const len = Math.hypot(nx, ny, nz) || 1.0;
    const ux = nx / len;
    const uy = ny / len;
    const uz = Math.max(-1.0, Math.min(1.0, nz / len));

    out.x = ux;
    out.y = uy;
    out.z = uz;
    out.ra = wrapRaDeg(Math.atan2(uy, ux) * RAD2DEG);
    out.dec = Math.asin(uz) * RAD2DEG;
    out.mag =
        a.mag !== null && b.mag !== null
            ? Number((a.mag + (b.mag - a.mag) * t).toFixed(1))
            : a.mag !== null
              ? a.mag
              : b.mag;
    out.distance_au =
        a.distance_au !== null && b.distance_au !== null
            ? Number((a.distance_au + (b.distance_au - a.distance_au) * t).toFixed(4))
            : a.distance_au !== null
              ? a.distance_au
              : b.distance_au;
    out.inRange = true;
    return out;
};

export const refreshEphemerides = async () => {
    if (ephemeridesFetchPromise) return ephemeridesFetchPromise;
    ephemeridesFetchPromise = (async () => {
        try {
            const resp = await fetch('/api/ephemeris/list');
            if (resp.ok) {
                const rawList = await resp.json();
                const prepared = (Array.isArray(rawList) ? rawList : [])
                    .map(prepareEphemerisRecord)
                    .filter(Boolean);
                savedEphemerides.value = prepared;
                ephemeridesLoadedOnce = true;
                ephemeridesVersion.value++;
            }
        } catch (err) {
            console.warn('Failed to load saved ephemerides:', err);
        } finally {
            ephemeridesFetchPromise = null;
        }
        return savedEphemerides.value;
    })();
    return ephemeridesFetchPromise;
};

export const ensureEphemeridesLoaded = () => {
    if (!ephemeridesLoadedOnce) {
        return refreshEphemerides();
    }
    return Promise.resolve(savedEphemerides.value);
};

export const upsertSavedEphemeris = (rawItem) => {
    const prepared = prepareEphemerisRecord(rawItem);
    if (!prepared) return null;
    const next = savedEphemerides.value.filter((e) => e.id !== prepared.id);
    next.push(prepared);
    savedEphemerides.value = next;
    ephemeridesVersion.value++;
    return prepared;
};

export const updateEphemerisItem = async (id, patch) => {
    const next = savedEphemerides.value.map((e) => (e.id === id ? { ...e, ...patch } : e));
    savedEphemerides.value = next;
    ephemeridesVersion.value++;
    try {
        await fetch(`/api/ephemeris/${encodeURIComponent(id)}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(patch)
        });
    } catch (err) {
        console.warn('Failed to persist ephemeris update:', err);
    }
};

export const deleteEphemerisItem = async (id) => {
    const resp = await fetch(`/api/ephemeris/${encodeURIComponent(id)}`, {
        method: 'DELETE'
    });
    if (!resp.ok) {
        throw new Error(`Failed to delete ephemeris (${resp.status})`);
    }
    savedEphemerides.value = savedEphemerides.value.filter((e) => e.id !== id);
    ephemeridesVersion.value++;
};
