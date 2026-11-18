<script setup>
import { ref, shallowRef, computed, watch, onMounted, onUnmounted } from 'vue';
import {
    skyAtlasVersion,
    skyAtlasSectorMode,
    skyAtlasLod0Sectors,
    skyAtlasLod1Sectors,
    skyAtlasLod2Sectors,
    skyAtlasItems,
    ensureSkyAtlasLoaded,
    ensureAtlasSheetImage
} from '../utils/skyAtlas.js';
import {
    computeLstDeg,
    parseObserverLocation,
    resolveObservingEvening,
    resolveLiveTonightEveningKey
} from '../utils/observingTime.js';
import ObservingDateConsole from './ObservingDateConsole.vue';

const props = defineProps({
    object: { type: Object, default: null },
    objects: { type: Array, default: () => [] },
    filteredObjects: { type: Array, default: null },
    settings: { type: Object, default: () => ({}) },
    clientSettings: { type: Object, default: () => ({}) },
    nightTimes: { type: Object, default: () => ({}) },
    sensorFov: { type: Object, default: () => ({ w: 0, h: 0 }) },
    comparisonRectangles: { type: Array, default: () => [] },
    sensorCenter: { type: Object, default: null },
    rotation: { type: Number, default: 0 },
    baseImageFov: { type: Number, default: 1.0 },
    initialMapFov: { type: Number, default: 35.0 },
    isLiveObsTime: { type: Boolean, default: undefined },
    liveNowMs: { type: Number, default: undefined },
    effectiveObsTimeMs: { type: Number, default: undefined }
});

const emit = defineEmits([
    'close',
    'select-object',
    'update-sensor-center',
    'reset-sensor-center',
    'update-obs-time'
]);

// --- MATH CONSTANTS ---
const DEG2RAD = Math.PI / 180.0;
const RAD2DEG = 180.0 / Math.PI;
const TAU = Math.PI * 2.0;

const STAR_COLORS = [
    [182, 212, 255], // 0: Blue-white O/B (Rigel, Alnitak)
    [220, 234, 255], // 1: Crisp white-blue A (Sirius, Vega)
    [255, 249, 238], // 2: Warm white F
    [255, 230, 184], // 3: Golden yellow G/K (Capella, Pollux)
    [255, 192, 136]  // 4: Orange-red K/M (Betelgeuse, Aldebaran)
];
const STAR_ALPHAS = [0.95, 0.68, 0.38];
const STAR_BUCKET_STYLES = [];
for (let c = 0; c < STAR_COLORS.length; c++) {
    const [r, g, b] = STAR_COLORS[c];
    for (let a = 0; a < STAR_ALPHAS.length; a++) {
        STAR_BUCKET_STYLES.push(`rgba(${r}, ${g}, ${b}, ${STAR_ALPHAS[a]})`);
    }
}

// --- MODULE-LEVEL CACHES (Persisted across Sky Map open/close) ---
let skyDataCache = null;
let skyDataPromise = null;
let preparedSky = null;
let starGlowSprites = null;
let mwCloudSprite = null;
const gridGeometryCache = new Map(); // tierIndex -> { meridians, parallels }
const textWidthCache = new Map();    // `${fontKey}|${text}` -> number
const imageElementCache = new Map(); // url -> { img, status: 'loading' | 'ready' | 'error' }
let inflightImageLoads = 0;
const MAX_CONCURRENT_INDIVIDUAL_LOADS = 6;

// --- 48-PATCH SPHERICAL BOUNDING-CONE PARTITION (6 Dec bands x 8 RA sectors) ---
const PATCH_DEC_BINS = 6;
const PATCH_RA_BINS = 8;
const PATCH_COUNT = PATCH_DEC_BINS * PATCH_RA_BINS;
const patchCx = new Float64Array(PATCH_COUNT);
const patchCy = new Float64Array(PATCH_COUNT);
const patchCz = new Float64Array(PATCH_COUNT);
const patchRadius = new Float64Array(PATCH_COUNT);
const patchCosR = new Float64Array(PATCH_COUNT);
const patchSinR = new Float64Array(PATCH_COUNT);
const visPatch = new Uint8Array(PATCH_COUNT);

(() => {
    const SAMPLES = 12;
    for (let dBin = 0; dBin < PATCH_DEC_BINS; dBin++) {
        const decMin = -90.0 + dBin * 30.0;
        const decMax = decMin + 30.0;
        for (let rBin = 0; rBin < PATCH_RA_BINS; rBin++) {
            const raMin = rBin * 45.0;
            const raMax = raMin + 45.0;
            const pid = dBin * PATCH_RA_BINS + rBin;

            let sx = 0;
            let sy = 0;
            let sz = 0;
            for (let di = 0; di <= SAMPLES; di++) {
                const d = (decMin + ((decMax - decMin) * di) / SAMPLES) * DEG2RAD;
                const cd = Math.cos(d);
                const sd = Math.sin(d);
                for (let ri = 0; ri <= SAMPLES; ri++) {
                    const a = (raMin + ((raMax - raMin) * ri) / SAMPLES) * DEG2RAD;
                    sx += cd * Math.cos(a);
                    sy += cd * Math.sin(a);
                    sz += sd;
                }
            }
            const len = Math.hypot(sx, sy, sz) || 1.0;
            const cx = sx / len;
            const cy = sy / len;
            const cz = sz / len;

            let minDot = 1.0;
            for (let di = 0; di <= SAMPLES; di++) {
                const d = (decMin + ((decMax - decMin) * di) / SAMPLES) * DEG2RAD;
                const cd = Math.cos(d);
                const sd = Math.sin(d);
                for (let ri = 0; ri <= SAMPLES; ri++) {
                    if (di > 0 && di < SAMPLES && ri > 0 && ri < SAMPLES) continue;
                    const a = (raMin + ((raMax - raMin) * ri) / SAMPLES) * DEG2RAD;
                    const dot = cx * cd * Math.cos(a) + cy * cd * Math.sin(a) + cz * sd;
                    if (dot < minDot) minDot = dot;
                }
            }
            // Add +0.065 rad (~3.7 deg) safety margin for wide-FOV tiles near patch borders
            const rad = Math.min(Math.PI, Math.acos(Math.max(-1.0, Math.min(1.0, minDot))) + 0.065);
            patchCx[pid] = cx;
            patchCy[pid] = cy;
            patchCz[pid] = cz;
            patchRadius[pid] = rad;
            patchCosR[pid] = Math.cos(rad);
            patchSinR[pid] = Math.sin(rad);
        }
    }
})();

const getSkyPatchId = (raDeg, decDeg) => {
    let r = Number(raDeg) % 360.0;
    if (r < 0) r += 360.0;
    const rBin = Math.min(PATCH_RA_BINS - 1, Math.max(0, (r / 45.0) | 0));
    const dClamped = Math.max(-90.0, Math.min(89.999, Number(decDeg)));
    const dBin = Math.min(PATCH_DEC_BINS - 1, Math.max(0, ((dClamped + 90.0) / 30.0) | 0));
    return dBin * PATCH_RA_BINS + rBin;
};

// Reusable zero-GC scratch buffers for per-frame horizon curves and dense-tile cell culling
const HORIZ_STEPS = 48;
const horizPtsX = new Float64Array(HORIZ_STEPS + 1);
const horizPtsY = new Float64Array(HORIZ_STEPS + 1);
let denseCellCounts = new Uint8Array(4096);

const wrapRaDeg = (ra) => {
    let r = Number(ra) % 360.0;
    if (r < 0) r += 360.0;
    return r;
};

const clampDecDeg = (dec) => Math.max(-89.99, Math.min(89.99, Number(dec)));
const clampAltDeg = (alt) => Math.max(-89.5, Math.min(89.5, Number(alt)));
const MS_PER_SIDEREAL_RAD = (86400000.0 * RAD2DEG) / 360.98564736629;

const SKY_FRAME_STORAGE_KEY = 'sky_observer_sky_frame_mode';
const readSavedSkyFrameMode = () => {
    try {
        const saved = window.localStorage?.getItem(SKY_FRAME_STORAGE_KEY);
        if (saved === 'horizon' || saved === 'sky') return saved;
    } catch {
        // Ignore storage errors
    }
    return 'sky';
};
const persistSkyFrameMode = (mode) => {
    try {
        window.localStorage?.setItem(SKY_FRAME_STORAGE_KEY, mode);
    } catch {
        // Ignore storage errors
    }
};

const SKY_APPLY_FILTERS_STORAGE_KEY = 'sky_observer_sky_apply_filters_v1';
const readSavedApplyFilters = () => {
    try {
        return window.localStorage?.getItem(SKY_APPLY_FILTERS_STORAGE_KEY) === '1';
    } catch {
        return false;
    }
};
const persistApplyFilters = (enabled) => {
    try {
        window.localStorage?.setItem(SKY_APPLY_FILTERS_STORAGE_KEY, enabled ? '1' : '0');
    } catch {
        // Ignore storage errors
    }
};

const SKY_FOV_PINNED_STORAGE_KEY = 'sky_observer_sky_fov_pinned_v2';
const readSavedFovPinned = () => {
    try {
        const raw = window.localStorage?.getItem(SKY_FOV_PINNED_STORAGE_KEY);
        if (raw === '0') return false;
        if (raw === '1') return true;
        return true;
    } catch {
        return true;
    }
};
const persistFovPinned = (pinned) => {
    try {
        window.localStorage?.setItem(SKY_FOV_PINNED_STORAGE_KEY, pinned ? '1' : '0');
    } catch {
        // Ignore storage errors
    }
};

const angularDistanceDeg = (ra1, dec1, ra2, dec2) => {
    const a1 = ra1 * DEG2RAD;
    const d1 = dec1 * DEG2RAD;
    const a2 = ra2 * DEG2RAD;
    const d2 = dec2 * DEG2RAD;
    const cosC =
        Math.sin(d1) * Math.sin(d2) +
        Math.cos(d1) * Math.cos(d2) * Math.cos(a2 - a1);
    return Math.acos(Math.max(-1.0, Math.min(1.0, cosC))) * RAD2DEG;
};

const formatRaHms = (raDeg) => {
    const norm = wrapRaDeg(raDeg);
    const totalSec = Math.round((norm / 15.0) * 3600.0);
    const h = Math.floor(totalSec / 3600) % 24;
    const m = Math.floor((totalSec % 3600) / 60);
    const s = totalSec % 60;
    return `${String(h).padStart(2, '0')}h ${String(m).padStart(2, '0')}m ${String(s).padStart(2, '0')}s`;
};

const formatDecDms = (decDeg) => {
    const clamped = Math.max(-90, Math.min(90, Number(decDeg)));
    const sign = clamped >= 0 ? '+' : '−';
    const totalArcmin = Math.round(Math.abs(clamped) * 60.0);
    const d = Math.floor(totalArcmin / 60);
    const m = totalArcmin % 60;
    return `${sign}${String(d).padStart(2, '0')}° ${String(m).padStart(2, '0')}′`;
};

const COMPASS_POINTS = [
    { label: 'N', azDeg: 0, major: true },
    { label: 'NE', azDeg: 45, major: false },
    { label: 'E', azDeg: 90, major: true },
    { label: 'SE', azDeg: 135, major: false },
    { label: 'S', azDeg: 180, major: true },
    { label: 'SW', azDeg: 225, major: false },
    { label: 'W', azDeg: 270, major: true },
    { label: 'NW', azDeg: 315, major: false }
].map((p) => ({
    ...p,
    sinAz: Math.sin(p.azDeg * DEG2RAD),
    cosAz: Math.cos(p.azDeg * DEG2RAD)
}));

const COMPASS_SECTORS_16 = [
    'N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
    'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'
];

const formatAltDeg = (altDeg) => {
    const sign = altDeg >= 0 ? '+' : '−';
    return `${sign}${Math.abs(altDeg).toFixed(1)}°`;
};

const formatAzCompass = (azDeg) => {
    const norm = wrapRaDeg(azDeg);
    const idx = Math.round(norm / 22.5) % 16;
    return `${Math.round(norm)}° ${COMPASS_SECTORS_16[idx]}`;
};

const makeHorizontalBasis = (latDeg, lonDeg, timeMs) => {
    const clampedLat = Math.max(-89.999, Math.min(89.999, latDeg));
    const phi = clampedLat * DEG2RAD;
    const theta = computeLstDeg(timeMs, lonDeg) * DEG2RAD;
    const sinP = Math.sin(phi);
    const cosP = Math.cos(phi);
    const sinT = Math.sin(theta);
    const cosT = Math.cos(theta);

    return {
        // Zenith unit vector Z
        Zx: cosP * cosT,
        Zy: cosP * sinT,
        Zz: sinP,
        // Horizontal due-North tangent vector Nh
        Nhx: -sinP * cosT,
        Nhy: -sinP * sinT,
        Nhz: cosP,
        // Horizontal due-East tangent vector Eh (Ehz = 0)
        Ehx: -sinT,
        Ehy: cosT
    };
};

const computeAltAzFromRaDec = (raDeg, decDeg, horiz) => {
    const a = raDeg * DEG2RAD;
    const d = decDeg * DEG2RAD;
    const cd = Math.cos(d);
    const x = cd * Math.cos(a);
    const y = cd * Math.sin(a);
    const z = Math.sin(d);

    const sinAlt = Math.max(-1.0, Math.min(1.0, x * horiz.Zx + y * horiz.Zy + z * horiz.Zz));
    const altDeg = Math.asin(sinAlt) * RAD2DEG;
    const eastComp = x * horiz.Ehx + y * horiz.Ehy;
    const northComp = x * horiz.Nhx + y * horiz.Nhy + z * horiz.Nhz;
    const azDeg = wrapRaDeg(Math.atan2(eastComp, northComp) * RAD2DEG);
    return { altDeg, azDeg };
};

const computeRaDecFromAltAz = (altDeg, azDeg, horiz) => {
    const alt = altDeg * DEG2RAD;
    const az = azDeg * DEG2RAD;
    const sinAlt = Math.sin(alt);
    const cosAlt = Math.cos(alt);
    const sinAz = Math.sin(az);
    const cosAz = Math.cos(az);
    const hx = cosAz * horiz.Nhx + sinAz * horiz.Ehx;
    const hy = cosAz * horiz.Nhy + sinAz * horiz.Ehy;
    const hz = cosAz * horiz.Nhz;
    const x = cosAlt * hx + sinAlt * horiz.Zx;
    const y = cosAlt * hy + sinAlt * horiz.Zy;
    const z = Math.max(-1.0, Math.min(1.0, cosAlt * hz + sinAlt * horiz.Zz));
    return {
        ra: wrapRaDeg(Math.atan2(y, x) * RAD2DEG),
        dec: clampDecDeg(Math.asin(z) * RAD2DEG)
    };
};

const getBvColorIndex = (bv) => {
    const v = Number.isFinite(bv) ? bv : 0.4;
    if (v < -0.1) return 0;
    if (v < 0.25) return 1;
    if (v < 0.6) return 2;
    if (v < 1.05) return 3;
    return 4;
};

const getMeasuredTextWidth = (ctx, fontKey, text) => {
    const key = `${fontKey}|${text}`;
    const cached = textWidthCache.get(key);
    if (cached !== undefined) return cached;
    const w = ctx.measureText(text).width;
    textWidthCache.set(key, w);
    return w;
};

// --- OFFSCREEN SPRITES & PRECOMPUTED 3D SPHERICAL GEOMETRY ---
const ensureSprites = () => {
    if (typeof document === 'undefined') return;

    if (!starGlowSprites) {
        starGlowSprites = STAR_COLORS.map(([r, g, b]) => {
            const sz = 48;
            const c = document.createElement('canvas');
            c.width = sz;
            c.height = sz;
            const gctx = c.getContext('2d');
            const half = sz * 0.5;
            const grad = gctx.createRadialGradient(half, half, 1.0, half, half, half);
            grad.addColorStop(0, `rgba(${r}, ${g}, ${b}, 0.52)`);
            grad.addColorStop(0.35, `rgba(${r}, ${g}, ${b}, 0.18)`);
            grad.addColorStop(1, `rgba(${r}, ${g}, ${b}, 0)`);
            gctx.fillStyle = grad;
            gctx.fillRect(0, 0, sz, sz);
            return c;
        });
    }

    if (!mwCloudSprite) {
        const sz = 96;
        const c = document.createElement('canvas');
        c.width = sz;
        c.height = sz;
        const gctx = c.getContext('2d');
        const half = sz * 0.5;
        const grad = gctx.createRadialGradient(half, half, 2, half, half, half);
        grad.addColorStop(0, 'rgba(154, 182, 236, 1.0)');
        grad.addColorStop(0.38, 'rgba(140, 168, 224, 0.52)');
        grad.addColorStop(0.72, 'rgba(125, 154, 212, 0.14)');
        grad.addColorStop(1, 'rgba(115, 145, 205, 0)');
        gctx.fillStyle = grad;
        gctx.fillRect(0, 0, sz, sz);
        mwCloudSprite = c;
    }
};

const buildBoundingSphere = (coords) => {
    const n = coords.length;
    const xyz = new Float32Array(n * 3);
    let sx = 0;
    let sy = 0;
    let sz = 0;

    for (let i = 0; i < n; i++) {
        const a = coords[i][0] * DEG2RAD;
        const d = coords[i][1] * DEG2RAD;
        const cd = Math.cos(d);
        const x = cd * Math.cos(a);
        const y = cd * Math.sin(a);
        const z = Math.sin(d);
        const o = i * 3;
        xyz[o] = x;
        xyz[o + 1] = y;
        xyz[o + 2] = z;
        sx += x;
        sy += y;
        sz += z;
    }

    const len = Math.hypot(sx, sy, sz);
    let cx = 0;
    let cy = 0;
    let cz = 1;
    let minDot = -1.0;

    if (len > 1e-6) {
        cx = sx / len;
        cy = sy / len;
        cz = sz / len;
        minDot = 1.0;
        for (let i = 0; i < n; i++) {
            const o = i * 3;
            const dot = xyz[o] * cx + xyz[o + 1] * cy + xyz[o + 2] * cz;
            if (dot < minDot) minDot = dot;
        }
    }

    const clampedDot = Math.max(-1.0, Math.min(1.0, minDot));
    const radiusRad = Math.acos(clampedDot);
    return {
        xyz,
        count: n,
        cx,
        cy,
        cz,
        radiusRad,
        cosR: clampedDot,
        sinR: Math.sin(radiusRad),
        pipMinDot: Math.cos(Math.min(Math.PI, radiusRad + 0.02))
    };
};

const buildMilkyWayNodes = () => {
    // J2000 Galactic-to-Equatorial basis constants
    const agp = 192.85948 * DEG2RAD;
    const dgp = 27.12825 * DEG2RAD;
    const lcp = 122.93192 * DEG2RAD;
    const sinDgp = Math.sin(dgp);
    const cosDgp = Math.cos(dgp);

    const galToXYZ = (lDeg, bDeg) => {
        const l = lDeg * DEG2RAD;
        const b = bDeg * DEG2RAD;
        const sinB = Math.sin(b);
        const cosB = Math.cos(b);
        const dl = lcp - l;
        const sinD = Math.max(-1.0, Math.min(1.0, sinB * sinDgp + cosB * cosDgp * Math.cos(dl)));
        const d = Math.asin(sinD);
        const y = cosB * Math.sin(dl);
        const x = cosDgp * sinB - sinDgp * cosB * Math.cos(dl);
        const a = agp + Math.atan2(y, x);
        const cd = Math.cos(d);
        return [cd * Math.cos(a), cd * Math.sin(a), sinD];
    };

    const nodes = [];
    // Spine + mottled flanking star-clouds around all 360° of the Galactic plane
    for (let i = 0; i < 90; i++) {
        const l = i * 4.0;
        // Brighter toward Galactic Center (l ~ 0°/360°), Cygnus (l ~ 80°), Carina/Crux (l ~ 290°)
        const gcWeight = Math.pow(Math.max(0, Math.cos(l * DEG2RAD)), 1.6);
        const cygWeight = Math.exp(-Math.pow((l - 78) / 28, 2)) * 0.45;
        const carWeight = Math.exp(-Math.pow((l - 292) / 26, 2)) * 0.5;
        const antiCenter = 0.32;
        const intensity = Math.min(1.0, antiCenter + gcWeight * 0.68 + cygWeight + carWeight);

        // Main diffuse Galactic spine
        const bJitter = Math.sin(i * 1.73) * 1.6;
        const [x0, y0, z0] = galToXYZ(l, bJitter);
        const rad0 = (9.5 + gcWeight * 6.5 + Math.cos(i * 1.1) * 1.8) * DEG2RAD;
        nodes.push(x0, y0, z0, rad0, 0.024 + intensity * 0.032);

        // Secondary flanking cloud puffs for natural mottled Milky Way texture
        const bFlank = (i % 2 === 0 ? 1 : -1) * (3.6 + Math.abs(Math.sin(i * 2.31)) * 3.4);
        const [x1, y1, z1] = galToXYZ(l + 2.0, bFlank);
        const rad1 = (6.8 + gcWeight * 4.2) * DEG2RAD;
        nodes.push(x1, y1, z1, rad1, 0.016 + intensity * 0.022);
    }

    return new Float32Array(nodes); // Stride 5: [x, y, z, radiusRad, alpha]
};

const prepareSkyGeometry = (data) => {
    if (!data) return null;
    if (preparedSky) return preparedSky;

    // 1. Stars -> Flat TypedArrays + compact bright & labeled subsets
    const rawStars = data.stars || [];
    const starCount = rawStars.length;
    const starXYZ = new Float32Array(starCount * 3);
    const starMag = new Float32Array(starCount);
    const starRadius = new Float32Array(starCount);
    const starBucket = new Uint8Array(starCount);
    const brightStars = [];
    const labeledStars = [];

    for (let i = 0; i < starCount; i++) {
        const st = rawStars[i];
        const a = st[0] * DEG2RAD;
        const d = st[1] * DEG2RAD;
        const mag = st[2];
        const cd = Math.cos(d);
        const o = i * 3;
        starXYZ[o] = cd * Math.cos(a);
        starXYZ[o + 1] = cd * Math.sin(a);
        starXYZ[o + 2] = Math.sin(d);
        starMag[i] = mag;

        const r = Math.max(0.55, Math.min(4.2, (5.9 - mag) * 0.56));
        starRadius[i] = r;

        const cIdx = getBvColorIndex(st[3]);
        const aIdx = mag < 2.6 ? 0 : mag < 4.5 ? 1 : 2;
        starBucket[i] = cIdx * 3 + aIdx;

        if (mag < 2.3) {
            brightStars.push({
                idx: i,
                mag,
                colorIdx: cIdx,
                haloMult: mag < 0.8 ? 4.4 : 3.1
            });
        }

        const properName = st[4] || '';
        const desig = st[5] || '';
        if ((properName || desig) && mag <= 4.45) {
            labeledStars.push({
                idx: i,
                mag,
                properName,
                desig,
                isBright: mag < 1.6
            });
        }
    }

    // 2. Constellation metadata & labels
    const nameById = new Map();
    const constLabels = [];
    for (const c of data.constellations || []) {
        nameById.set(c.id, c.name);
        const a = c.ra * DEG2RAD;
        const d = c.dec * DEG2RAD;
        const cd = Math.cos(d);
        constLabels.push({
            id: c.id,
            name: c.name,
            tracked: String(c.name).toUpperCase().split('').join(' '),
            rank: c.rank || 2,
            x: cd * Math.cos(a),
            y: cd * Math.sin(a),
            z: Math.sin(d)
        });
    }

    // 3. Constellation stick line segments with 3D bounding spheres
    const constLines = [];
    for (const c of data.constellation_lines || []) {
        for (const seg of c.lines || []) {
            if (!seg || seg.length < 2) continue;
            constLines.push(buildBoundingSphere(seg));
        }
    }

    // 4. Constellation boundary rings with 3D bounding spheres (used for drawing & instant lookup)
    const constBounds = [];
    for (const b of data.constellation_bounds || []) {
        const cname = nameById.get(b.id) || b.id;
        for (const ring of b.rings || []) {
            if (!ring || ring.length < 3) continue;
            const sphere = buildBoundingSphere(ring);
            sphere.id = b.id;
            sphere.name = cname;
            constBounds.push(sphere);
        }
    }

    // 5. Milky Way 3D cloud nodes
    const mwNodes = buildMilkyWayNodes();

    preparedSky = {
        starCount,
        starXYZ,
        starMag,
        starRadius,
        starBucket,
        brightStars,
        labeledStars,
        constLabels,
        constLines,
        constBounds,
        mwNodes
    };
    return preparedSky;
};

const getGridGeometry = (fov) => {
    const tier = fov > 60 ? 0 : fov > 24 ? 1 : fov > 10 ? 2 : fov > 4 ? 3 : 4;
    const cached = gridGeometryCache.get(tier);
    if (cached) return cached;

    const raSteps = [30.0, 15.0, 7.5, 3.75, 1.25];
    const decSteps = [15.0, 10.0, 5.0, 2.0, 1.0];
    const decSamples = [4.0, 3.0, 2.0, 1.25, 0.75];
    const raSamples = [4.5, 3.5, 2.0, 1.25, 0.75];

    const raStepDeg = raSteps[tier];
    const decStepDeg = decSteps[tier];
    const decSample = decSamples[tier];
    const raSample = raSamples[tier];

    const meridians = [];
    const mCount = Math.floor(170.0 / decSample) + 1;
    for (let ra = 0; ra < 360 - 1e-5; ra += raStepDeg) {
        const a = ra * DEG2RAD;
        const ca = Math.cos(a);
        const sa = Math.sin(a);
        const xyz = new Float32Array(mCount * 3);
        let idx = 0;
        for (let k = 0; k < mCount; k++) {
            const dec = Math.min(85.0, -85.0 + k * decSample);
            const d = dec * DEG2RAD;
            const cd = Math.cos(d);
            xyz[idx++] = cd * ca;
            xyz[idx++] = cd * sa;
            xyz[idx++] = Math.sin(d);
        }
        meridians.push({ ra, xyz, count: mCount });
    }

    const parallels = [];
    const pCount = Math.floor(360.0 / raSample) + 1;
    for (let dec = -80; dec <= 80.001; dec += decStepDeg) {
        const d = dec * DEG2RAD;
        const cd = Math.cos(d);
        const sd = Math.sin(d);
        const xyz = new Float32Array(pCount * 3);
        let idx = 0;
        for (let k = 0; k < pCount; k++) {
            const ra = k === pCount - 1 ? 0.0 : k * raSample;
            const a = ra * DEG2RAD;
            xyz[idx++] = cd * Math.cos(a);
            xyz[idx++] = cd * Math.sin(a);
            xyz[idx++] = sd;
        }
        parallels.push({
            dec,
            isEquator: Math.abs(dec) < 0.01,
            xyz,
            count: pCount
        });
    }

    const built = { meridians, parallels };
    gridGeometryCache.set(tier, built);
    return built;
};

const loadSkyData = async () => {
    if (preparedSky) return skyDataCache;
    if (!skyDataPromise) {
        skyDataPromise = fetch('/sky_data.json')
            .then((r) => {
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                return r.json();
            })
            .then((data) => {
                skyDataCache = data;
                prepareSkyGeometry(data);
                return data;
            })
            .catch((err) => {
                skyDataPromise = null;
                console.error('Failed to load sky_data.json:', err);
                return null;
            });
    }
    return skyDataPromise;
};

// Pre-allocated star render buckets (reused every frame without garbage collection)
const starBuckets = Array.from({ length: STAR_BUCKET_STYLES.length }, () => []);

// --- REACTIVE STATE ---
const containerRef = ref(null);
const canvasRef = ref(null);
const viewportW = ref(800);
const viewportH = ref(600);

const getCatalogTargetCoords = () => {
    if (!props.object) return { ra: 83.822, dec: -5.391 };
    const ra =
        props.object.center_ra !== undefined
            ? props.object.center_ra
            : props.object.ra;
    const dec =
        props.object.center_dec !== undefined
            ? props.object.center_dec
            : props.object.dec;
    return {
        ra: wrapRaDeg(parseFloat(ra) || 0),
        dec: clampDecDeg(parseFloat(dec) || 0)
    };
};

const getPropSensorCoords = () => {
    if (!props.object) return { ra: 83.822, dec: -5.391 };
    const sRa = Number(props.sensorCenter?.ra);
    const sDec = Number(props.sensorCenter?.dec);
    if (Number.isFinite(sRa) && Number.isFinite(sDec)) {
        return {
            ra: wrapRaDeg(sRa),
            dec: clampDecDeg(sDec)
        };
    }
    return getCatalogTargetCoords();
};

const initialCoords = getPropSensorCoords();
const viewRa = ref(initialCoords.ra);
const viewDec = ref(initialCoords.dec);
const mapFovDeg = ref(Math.max(0.4, Math.min(100.0, Number(props.initialMapFov) || 35.0)));

// Sensor FOV interactive state (pinned to sky vs unpinned centered in viewport)
const fovRa = ref(initialCoords.ra);
const fovDec = ref(initialCoords.dec);
const fovPinned = ref(readSavedFovPinned());
const isHoveringFov = ref(false);
const isDraggingFov = ref(false);
const fovCtrlRef = ref(null);
const fovOverlayVisible = ref(false);
let fovOverlayLeft = 0;
let fovOverlayTop = 0;
let lastEmittedFov = { ra: initialCoords.ra, dec: initialCoords.dec };
const lastFovHit = {
    valid: false,
    cx: 0,
    cy: 0,
    halfW: 14,
    halfH: 14,
    cosR: 1.0,
    sinR: 0.0
};
let fovDragOffset = { dx: 0, dy: 0 };

const hasValidSensorFov = computed(() =>
    Boolean(props.object && Number(props.sensorFov?.w) > 0 && Number(props.sensorFov?.h) > 0)
);

const isFovDetached = computed(() => {
    if (!hasValidSensorFov.value) return false;
    const cat = getCatalogTargetCoords();
    return angularDistanceDeg(fovRa.value, fovDec.value, cat.ra, cat.dec) > 0.03;
});

// Layer visibility toggles
const showCachedPhotos = ref(true);
const showConstellations = ref(true);
const showGrid = ref(true);
const showDsos = ref(true);
const showHorizon = ref(true);
const applyTargetFilters = ref(readSavedApplyFilters());

const filteredNameSet = computed(() => {
    const list = Array.isArray(props.filteredObjects) ? props.filteredObjects : props.objects;
    const set = new Set();
    if (Array.isArray(list)) {
        for (let i = 0; i < list.length; i++) {
            const name = list[i]?.name;
            if (name) set.add(name);
        }
    }
    return set;
});

const filteredTargetCount = computed(() => filteredNameSet.value.size);

const toggleApplyTargetFilters = () => {
    const next = !applyTargetFilters.value;
    applyTargetFilters.value = next;
    persistApplyFilters(next);
    if (next && !showDsos.value) {
        showDsos.value = true;
    }
    if (next && hoveredItem.value && !filteredNameSet.value.has(hoveredItem.value.name)) {
        hoveredItem.value = null;
    }
    scheduleDraw();
};

// Observer Location, Minimum Altitude & Shared Observation Time State
const observerLocation = computed(() => parseObserverLocation(props.settings));

const minAltitudeDeg = computed(() => {
    const raw = props.clientSettings?.min_hours_altitude ?? props.settings?.min_altitude ?? 30;
    const val = Number(raw);
    return Number.isFinite(val) ? Math.max(0, Math.min(88, val)) : 30;
});

const isLiveTime = ref(props.isLiveObsTime ?? true);
const liveNowMs = ref(Number.isFinite(props.liveNowMs) ? props.liveNowMs : Date.now());
const scrubTimeMs = ref(
    Number.isFinite(props.effectiveObsTimeMs) ? props.effectiveObsTimeMs : Date.now()
);
const effectiveTimeMs = computed(() => (isLiveTime.value ? liveNowMs.value : scrubTimeMs.value));

// Reference Frame Mode: 'sky' (Equatorial RA/Dec fixed) vs 'horizon' (Horizontal Alt-Az fixed)
const skyFrameMode = ref(readSavedSkyFrameMode());
if (skyFrameMode.value === 'horizon') {
    showHorizon.value = true;
}
const dragTimeMode = ref(false);
const isShiftHeld = ref(false);
const viewAz = ref(180.0);
const viewAlt = ref(35.0);
let viewHorizSynced = false;

const isHorizonFixed = computed(
    () => skyFrameMode.value === 'horizon' && Boolean(observerLocation.value)
);
const isTimeDragActive = computed(
    () => isHorizonFixed.value && (dragTimeMode.value || isShiftHeld.value)
);

const syncHorizFromRaDec = (
    raDeg = viewRa.value,
    decDeg = viewDec.value,
    timeMs = effectiveTimeMs.value
) => {
    const loc = observerLocation.value;
    if (!loc) return false;
    const horiz = makeHorizontalBasis(loc.lat, loc.lon, timeMs);
    const { altDeg, azDeg } = computeAltAzFromRaDec(raDeg, decDeg, horiz);
    viewAz.value = azDeg;
    viewAlt.value = clampAltDeg(altDeg);
    viewHorizSynced = true;
    return true;
};
syncHorizFromRaDec(initialCoords.ra, initialCoords.dec, effectiveTimeMs.value);

const getCurrentCenterRaDec = () => {
    if (isHorizonFixed.value && observerLocation.value) {
        const loc = observerLocation.value;
        const horiz = makeHorizontalBasis(loc.lat, loc.lon, effectiveTimeMs.value);
        return computeRaDecFromAltAz(viewAlt.value, viewAz.value, horiz);
    }
    return { ra: viewRa.value, dec: viewDec.value };
};

const isLoadingSky = ref(!preparedSky);
const cachedServerImages = shallowRef([]);
const skyPhotoCount = ref(0);
const hoveredItem = ref(null);

// Throttled status bar readout refs (decoupled from high-frequency mousemove events)
const readoutRaText = ref(formatRaHms(initialCoords.ra));
const readoutDecText = ref(formatDecDms(initialCoords.dec));
const readoutAltText = ref('');
const readoutAzText = ref('');
const readoutBelowHorizon = ref(false);
const readoutConstellation = ref('');
const readoutFovText = computed(() => {
    const v = mapFovDeg.value;
    return v >= 10 ? `${Math.round(v)}°` : `${v.toFixed(1)}°`;
});

let canvasCtx = null;
let cachedBgGrad = null;
let cachedBgKey = '';
let rafId = null;
let pointerRafId = null;
let animFrameId = null;
let liveClockTimer = null;
let resizeObserver = null;
let lastReadoutTs = 0;
let lastReadoutCoords = { ra: initialCoords.ra, dec: initialCoords.dec };

// Cached canvas screen origin (avoids synchronous getBoundingClientRect() layout reflows on mousemove/wheel)
let canvasLeft = 0;
let canvasTop = 0;
let canvasRectValid = false;
const refreshCanvasRect = () => {
    const el = canvasRef.value;
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    canvasLeft = rect.left;
    canvasTop = rect.top;
    canvasRectValid = rect.width > 0 && rect.height > 0;
    return canvasRectValid;
};

// Coalesced pointer state & reusable zero-GC per-frame pools
const isPanning = ref(false);
let panMoved = false;
let panDragKind = 'eq'; // 'eq' | 'altaz' | 'time'
let panStartScreen = { x: 0, y: 0 };
let panStartSky = { ra: 0, dec: 0 };
let panStartHoriz = { az: 180.0, alt: 35.0 };
let panStartTimeMs = 0;
let panTimeVel = { vx: 1.0, vy: 0.0, denom: 1.0, f: 400.0 };
let pendingPointer = null; // { px, py, clientX, clientY }
let pinchStartDist = 0;
let pinchStartFov = 35.0;

const interactivePool = [];
let interactiveCount = 0;
const pushInteractiveItem = (
    kind,
    name,
    commonName,
    fov,
    ra,
    dec,
    x,
    y,
    hitR,
    refObj = null,
    isCustom = false,
    bx1 = 0,
    by1 = 0,
    bx2 = 0,
    by2 = 0
) => {
    let slot = interactivePool[interactiveCount];
    if (!slot) {
        slot = {
            kind,
            name,
            commonName,
            fov,
            ra,
            dec,
            x,
            y,
            hitR,
            refObj,
            isCustom,
            bx1,
            by1,
            bx2,
            by2
        };
        interactivePool[interactiveCount] = slot;
    } else {
        slot.kind = kind;
        slot.name = name;
        slot.commonName = commonName;
        slot.fov = fov;
        slot.ra = ra;
        slot.dec = dec;
        slot.x = x;
        slot.y = y;
        slot.hitR = hitR;
        slot.refObj = refObj;
        slot.isCustom = isCustom;
        slot.bx1 = bx1;
        slot.by1 = by1;
        slot.bx2 = bx2;
        slot.by2 = by2;
    }
    interactiveCount++;
};

const placedBoxesPool = [];
let placedBoxesCount = 0;
const pushPlacedBox = (x1, y1, x2, y2) => {
    let slot = placedBoxesPool[placedBoxesCount];
    if (!slot) {
        slot = { x1, y1, x2, y2 };
        placedBoxesPool[placedBoxesCount] = slot;
    } else {
        slot.x1 = x1;
        slot.y1 = y1;
        slot.x2 = x2;
        slot.y2 = y2;
    }
    placedBoxesCount++;
};

const visibleDsosPool = [];
let visibleDsosCount = 0;
const pushVisibleDso = (d, sx, sy, rPx) => {
    let slot = visibleDsosPool[visibleDsosCount];
    if (!slot) {
        slot = { d, sx, sy, rPx };
        visibleDsosPool[visibleDsosCount] = slot;
    } else {
        slot.d = d;
        slot.sx = sx;
        slot.sy = sy;
        slot.rPx = rPx;
    }
    visibleDsosCount++;
};

// --- FAST CAMERA BASIS & PROJECTION ---
const makeCameraBasis = (centerRaDeg, centerDecDeg, w, h, fovDeg) => {
    const a0 = centerRaDeg * DEG2RAD;
    const d0 = centerDecDeg * DEG2RAD;
    const sinA0 = Math.sin(a0);
    const cosA0 = Math.cos(a0);
    const sinD0 = Math.sin(d0);
    const cosD0 = Math.cos(d0);
    const fovRad = Math.max(0.25, Math.min(125.0, fovDeg)) * DEG2RAD;
    const f = (Math.max(h, 120) * 0.5) / Math.tan(fovRad * 0.5);

    return {
        f,
        halfW: w * 0.5,
        halfH: h * 0.5,
        // View center unit vector C
        Cx: cosD0 * cosA0,
        Cy: cosD0 * sinA0,
        Cz: sinD0,
        // Screen-left tangent vector E (Celestial East)
        Ex: -sinA0,
        Ey: cosA0,
        Ez: 0.0,
        // Screen-up tangent vector N (Celestial North)
        Nx: -sinD0 * cosA0,
        Ny: -sinD0 * sinA0,
        Nz: cosD0
    };
};

const makeAltAzCameraBasis = (centerAzDeg, centerAltDeg, horiz, w, h, fovDeg) => {
    const az = centerAzDeg * DEG2RAD;
    const alt = clampAltDeg(centerAltDeg) * DEG2RAD;
    const sinAz = Math.sin(az);
    const cosAz = Math.cos(az);
    const sinAlt = Math.sin(alt);
    const cosAlt = Math.cos(alt);
    const fovRad = Math.max(0.25, Math.min(125.0, fovDeg)) * DEG2RAD;
    const f = (Math.max(h, 120) * 0.5) / Math.tan(fovRad * 0.5);

    // Horizontal look unit vector H(az) = cos(az)*Nh + sin(az)*Eh (Ehz = 0)
    const hx = cosAz * horiz.Nhx + sinAz * horiz.Ehx;
    const hy = cosAz * horiz.Nhy + sinAz * horiz.Ehy;
    const hz = cosAz * horiz.Nhz;

    return {
        f,
        halfW: w * 0.5,
        halfH: h * 0.5,
        // View center unit vector C = cos(alt)*H + sin(alt)*Z
        Cx: cosAlt * hx + sinAlt * horiz.Zx,
        Cy: cosAlt * hy + sinAlt * horiz.Zy,
        Cz: cosAlt * hz + sinAlt * horiz.Zz,
        // Screen-left tangent vector E = sin(az)*Nh - cos(az)*Eh (strictly orthogonal to Zenith Z)
        Ex: sinAz * horiz.Nhx - cosAz * horiz.Ehx,
        Ey: sinAz * horiz.Nhy - cosAz * horiz.Ehy,
        Ez: sinAz * horiz.Nhz,
        // Screen-up tangent vector N = -sin(alt)*H + cos(alt)*Z (toward local Zenith)
        Nx: -sinAlt * hx + cosAlt * horiz.Zx,
        Ny: -sinAlt * hy + cosAlt * horiz.Zy,
        Nz: -sinAlt * hz + cosAlt * horiz.Zz
    };
};

const getActiveCameraBasis = (
    w = viewportW.value,
    h = viewportH.value,
    fovDeg = mapFovDeg.value,
    timeMs = effectiveTimeMs.value
) => {
    const loc = observerLocation.value;
    if (isHorizonFixed.value && loc) {
        const horiz = makeHorizontalBasis(loc.lat, loc.lon, timeMs);
        return makeAltAzCameraBasis(viewAz.value, viewAlt.value, horiz, w, h, fovDeg);
    }
    return makeCameraBasis(viewRa.value, viewDec.value, w, h, fovDeg);
};

const projectRaDecWithBasis = (raDeg, decDeg, cam) => {
    const a = raDeg * DEG2RAD;
    const d = decDeg * DEG2RAD;
    const cd = Math.cos(d);
    const x = cd * Math.cos(a);
    const y = cd * Math.sin(a);
    const z = Math.sin(d);
    const cosC = x * cam.Cx + y * cam.Cy + z * cam.Cz;
    if (cosC <= -0.35) return null;
    const k = 2.0 / (1.0 + cosC);
    const fk = cam.f * k;
    return {
        x: cam.halfW - fk * (x * cam.Ex + y * cam.Ey + z * cam.Ez),
        y: cam.halfH - fk * (x * cam.Nx + y * cam.Ny + z * cam.Nz),
        k,
        cosC,
        f: cam.f
    };
};

const computeLocalNorthAngleWithBasis = (raDeg, decDeg, projPoint, cam) => {
    const p = projPoint || projectRaDecWithBasis(raDeg, decDeg, cam);
    if (!p) return 0;
    const step = decDeg > 89.0 ? -0.05 : 0.05;
    const pn = projectRaDecWithBasis(raDeg, decDeg + step, cam);
    if (!pn) return 0;
    const dx = step > 0 ? pn.x - p.x : p.x - pn.x;
    const dy = step > 0 ? pn.y - p.y : p.y - pn.y;
    return Math.atan2(dx, -dy);
};

const screenToUnitVec = (px, py, cam = getActiveCameraBasis()) => {
    const u = -(px - cam.halfW) / cam.f;
    const v = -(py - cam.halfH) / cam.f;
    const rhoSq = u * u + v * v;
    const invDenom = 1.0 / (4.0 + rhoSq);
    const wC = (4.0 - rhoSq) * invDenom;
    const wE = 4.0 * u * invDenom;
    const wN = 4.0 * v * invDenom;
    const x = wC * cam.Cx + wE * cam.Ex + wN * cam.Nx;
    const y = wC * cam.Cy + wE * cam.Ey + wN * cam.Ny;
    const z = Math.max(-1.0, Math.min(1.0, wC * cam.Cz + wE * cam.Ez + wN * cam.Nz));
    return { x, y, z, cosC: wC };
};

const screenToSky = (px, py, customCam = null) => {
    const cam = customCam || getActiveCameraBasis();
    const p = screenToUnitVec(px, py, cam);
    return {
        ra: wrapRaDeg(Math.atan2(p.y, p.x) * RAD2DEG),
        dec: clampDecDeg(Math.asin(p.z) * RAD2DEG)
    };
};

const screenToEquatorialPan = (px, py, centerRaDeg, centerDecDeg) => {
    const w = viewportW.value;
    const h = Math.max(viewportH.value, 120);
    const fovRad = Math.max(0.25, Math.min(125.0, mapFovDeg.value)) * DEG2RAD;
    const f = (h * 0.5) / Math.tan(fovRad * 0.5);
    const u = -(px - w * 0.5) / f;
    const v = -(py - viewportH.value * 0.5) / f;
    const rho = Math.hypot(u, v);

    if (rho < 1e-9) {
        return { ra: wrapRaDeg(centerRaDeg), dec: clampDecDeg(centerDecDeg) };
    }

    const c = 2.0 * Math.atan(rho * 0.5);
    const sinC = Math.sin(c);
    const cosC = Math.cos(c);
    const a0 = centerRaDeg * DEG2RAD;
    const d0 = centerDecDeg * DEG2RAD;
    const sinD0 = Math.sin(d0);
    const cosD0 = Math.cos(d0);

    const sinD = Math.max(-1.0, Math.min(1.0, cosC * sinD0 + (v * sinC * cosD0) / rho));
    const dec = Math.asin(sinD) * RAD2DEG;
    const da = Math.atan2(u * sinC, rho * cosD0 * cosC - v * sinD0 * sinC);
    const ra = wrapRaDeg((a0 + da) * RAD2DEG);

    return { ra, dec: clampDecDeg(dec) };
};

const screenToAltAz = (px, py, centerAzDeg = viewAz.value, centerAltDeg = viewAlt.value) => {
    const w = viewportW.value;
    const h = Math.max(viewportH.value, 120);
    const fovRad = Math.max(0.25, Math.min(125.0, mapFovDeg.value)) * DEG2RAD;
    const f = (h * 0.5) / Math.tan(fovRad * 0.5);
    const u = -(px - w * 0.5) / f;
    const v = -(py - viewportH.value * 0.5) / f;
    const rho = Math.hypot(u, v);

    if (rho < 1e-9) {
        return { az: wrapRaDeg(centerAzDeg), alt: clampAltDeg(centerAltDeg) };
    }

    const c = 2.0 * Math.atan(rho * 0.5);
    const sinC = Math.sin(c);
    const cosC = Math.cos(c);
    const alt0 = clampAltDeg(centerAltDeg) * DEG2RAD;
    const sinAlt0 = Math.sin(alt0);
    const cosAlt0 = Math.cos(alt0);

    const sinAlt = Math.max(-1.0, Math.min(1.0, cosC * sinAlt0 + (v * sinC * cosAlt0) / rho));
    const alt = clampAltDeg(Math.asin(sinAlt) * RAD2DEG);
    // +u is screen LEFT = decreasing Azimuth
    const dAz = -Math.atan2(u * sinC, rho * cosAlt0 * cosC - v * sinAlt0 * sinC);
    const az = wrapRaDeg(centerAzDeg + dAz * RAD2DEG);

    return { az, alt };
};

// --- SENSOR FOV PIN TO SKY / UNPIN TO VIEWPORT CENTER & FRAMING SYNC ---
const emitSensorCenterIfChanged = (persist = false) => {
    if (!props.object) return;
    const ra = wrapRaDeg(fovRa.value);
    const dec = clampDecDeg(fovDec.value);
    if (
        persist ||
        Math.abs(ra - lastEmittedFov.ra) > 1e-5 ||
        Math.abs(dec - lastEmittedFov.dec) > 1e-5
    ) {
        lastEmittedFov = { ra, dec };
        emit('update-sensor-center', persist ? { ra, dec, persist: true } : { ra, dec });
    }
};

const syncUnpinnedFovToCenter = (cam = getActiveCameraBasis()) => {
    if (fovPinned.value || !hasValidSensorFov.value) return;
    fovRa.value = wrapRaDeg(Math.atan2(cam.Cy, cam.Cx) * RAD2DEG);
    fovDec.value = clampDecDeg(Math.asin(Math.max(-1.0, Math.min(1.0, cam.Cz))) * RAD2DEG);
};

const hitTestSensorFov = (px, py) => {
    // Only capture direct FOV drag when pinned to the sky; when unpinned, FOV stays
    // locked at viewport center so dragging anywhere pans the sky underneath it.
    if (!fovPinned.value || !hasValidSensorFov.value || !lastFovHit.valid) {
        return false;
    }
    const dx = px - lastFovHit.cx;
    const dy = py - lastFovHit.cy;
    const lx = dx * lastFovHit.cosR + dy * lastFovHit.sinR;
    const ly = -dx * lastFovHit.sinR + dy * lastFovHit.cosR;
    return Math.abs(lx) <= lastFovHit.halfW && Math.abs(ly) <= lastFovHit.halfH;
};

const updateFovFloatingCtrlPosition = (leftPx, topPx, visible) => {
    if (fovOverlayVisible.value !== visible) {
        fovOverlayVisible.value = visible;
    }
    if (!visible) return;
    const rx = Math.round(leftPx);
    const ry = Math.round(topPx);
    fovOverlayLeft = rx;
    fovOverlayTop = ry;
    const el = fovCtrlRef.value;
    if (el) {
        el.style.transform = `translate3d(${rx}px, ${ry}px, 0)`;
    }
};

const toggleFovPin = () => {
    const next = !fovPinned.value;
    fovPinned.value = next;
    persistFovPinned(next);
    if (!next) {
        isDraggingFov.value = false;
        isHoveringFov.value = false;
        const cam = getActiveCameraBasis();
        syncUnpinnedFovToCenter(cam);
        emitSensorCenterIfChanged(true);
    } else {
        emitSensorCenterIfChanged(true);
    }
    scheduleDraw();
};

const resetSensorFovToTarget = () => {
    const cat = getCatalogTargetCoords();
    fovRa.value = cat.ra;
    fovDec.value = cat.dec;
    lastEmittedFov = { ra: cat.ra, dec: cat.dec };
    if (!fovPinned.value) {
        if (animFrameId) {
            cancelAnimationFrame(animFrameId);
            animFrameId = null;
        }
        viewRa.value = cat.ra;
        viewDec.value = cat.dec;
        syncHorizFromRaDec(cat.ra, cat.dec, effectiveTimeMs.value);
    }
    emit('reset-sensor-center');
    updateStatusReadout(cat.ra, cat.dec, true);
    scheduleDraw();
};

// --- ULTRA-FAST 3D BOUNDING-SPHERE CONSTELLATION LOOKUP (< 0.02 ms) ---
const findConstellationName = (raDeg, decDeg) => {
    if (!preparedSky) return '';
    const a0 = raDeg * DEG2RAD;
    const d0 = decDeg * DEG2RAD;
    const sinA0 = Math.sin(a0);
    const cosA0 = Math.cos(a0);
    const sinD0 = Math.sin(d0);
    const cosD0 = Math.cos(d0);

    // Query unit vector Q and tangent basis E, N
    const qx = cosD0 * cosA0;
    const qy = cosD0 * sinA0;
    const qz = sinD0;
    const ex = -sinA0;
    const ey = cosA0;
    const nx = -sinD0 * cosA0;
    const ny = -sinD0 * sinA0;
    const nz = cosD0;

    const bounds = preparedSky.constBounds;
    for (let b = 0; b < bounds.length; b++) {
        const ring = bounds[b];
        // Single dot product rejects ~97% of constellation rings immediately
        if (qx * ring.cx + qy * ring.cy + qz * ring.cz < ring.pipMinDot) continue;

        const xyz = ring.xyz;
        const n = ring.count;
        const lastO = (n - 1) * 3;
        const prevCos = xyz[lastO] * qx + xyz[lastO + 1] * qy + xyz[lastO + 2] * qz;
        if (prevCos <= 0.05) continue;

        let xj = (xyz[lastO] * ex + xyz[lastO + 1] * ey) / prevCos;
        let yj = (xyz[lastO] * nx + xyz[lastO + 1] * ny + xyz[lastO + 2] * nz) / prevCos;
        let inside = false;
        let valid = true;

        for (let i = 0; i < n; i++) {
            const o = i * 3;
            const vx = xyz[o];
            const vy = xyz[o + 1];
            const vz = xyz[o + 2];
            const cosC = vx * qx + vy * qy + vz * qz;
            if (cosC <= 0.05) {
                valid = false;
                break;
            }
            const xi = (vx * ex + vy * ey) / cosC;
            const yi = (vx * nx + vy * ny + vz * nz) / cosC;
            if (yi > 0 !== yj > 0 && 0 < ((xj - xi) * -yi) / (yj - yi + 1e-15) + xi) {
                inside = !inside;
            }
            xj = xi;
            yj = yi;
        }

        if (valid && inside) {
            return ring.name;
        }
    }

    // Fallback: closest constellation centroid by dot product
    let bestName = '';
    let bestDot = -2.0;
    const labels = preparedSky.constLabels;
    for (let i = 0; i < labels.length; i++) {
        const c = labels[i];
        const dot = qx * c.x + qy * c.y + qz * c.z;
        if (dot > bestDot) {
            bestDot = dot;
            bestName = c.name;
        }
    }
    return bestName;
};

const updateStatusReadout = (raDeg, decDeg, force = false) => {
    const now = performance.now();
    if (!force && now - lastReadoutTs < 65) return;
    lastReadoutTs = now;
    lastReadoutCoords = { ra: raDeg, dec: decDeg };
    readoutRaText.value = formatRaHms(raDeg);
    readoutDecText.value = formatDecDms(decDeg);
    if (force || !isPanning.value) {
        readoutConstellation.value = findConstellationName(raDeg, decDeg);
    }

    const loc = observerLocation.value;
    if (loc) {
        const horiz = makeHorizontalBasis(loc.lat, loc.lon, effectiveTimeMs.value);
        const { altDeg, azDeg } = computeAltAzFromRaDec(raDeg, decDeg, horiz);
        readoutAltText.value = formatAltDeg(altDeg);
        readoutAzText.value = formatAzCompass(azDeg);
        readoutBelowHorizon.value = altDeg < 0;
    } else {
        readoutAltText.value = '';
        readoutAzText.value = '';
        readoutBelowHorizon.value = false;
    }
};

// --- OBSERVATION TIME & REFERENCE FRAME CONTROLS ---
const refreshReadoutAfterTimeChange = () => {
    if (isHorizonFixed.value) {
        const cam = getActiveCameraBasis();
        if (!fovPinned.value && hasValidSensorFov.value) {
            syncUnpinnedFovToCenter(cam);
            emitSensorCenterIfChanged();
        }
        if (pendingPointer && Number.isFinite(pendingPointer.px) && Number.isFinite(pendingPointer.py)) {
            const pt = screenToSky(pendingPointer.px, pendingPointer.py, cam);
            updateStatusReadout(pt.ra, pt.dec, true);
        } else {
            const ctr = getCurrentCenterRaDec();
            updateStatusReadout(ctr.ra, ctr.dec, true);
        }
    } else {
        updateStatusReadout(lastReadoutCoords.ra, lastReadoutCoords.dec, true);
    }
};

const setSkyFrameMode = (mode) => {
    const targetMode = mode === 'horizon' ? 'horizon' : 'sky';
    if (targetMode === skyFrameMode.value) return;

    if (animFrameId) {
        cancelAnimationFrame(animFrameId);
        animFrameId = null;
    }

    if (targetMode === 'horizon') {
        if (!observerLocation.value) return;
        syncHorizFromRaDec(viewRa.value, viewDec.value, effectiveTimeMs.value);
        showHorizon.value = true;
        skyFrameMode.value = 'horizon';
        persistSkyFrameMode('horizon');
    } else {
        const ctr = getCurrentCenterRaDec();
        viewRa.value = ctr.ra;
        viewDec.value = ctr.dec;
        dragTimeMode.value = false;
        skyFrameMode.value = 'sky';
        persistSkyFrameMode('sky');
    }

    if (!fovPinned.value && hasValidSensorFov.value) {
        syncUnpinnedFovToCenter();
        emitSensorCenterIfChanged();
    }

    const ctr = getCurrentCenterRaDec();
    updateStatusReadout(ctr.ra, ctr.dec, true);
    scheduleDraw();
};

const toggleSkyFrameMode = () => {
    setSkyFrameMode(isHorizonFixed.value ? 'sky' : 'horizon');
};

const toggleHorizonLayer = () => {
    if (showHorizon.value) {
        if (isHorizonFixed.value) {
            setSkyFrameMode('sky');
        }
        showHorizon.value = false;
    } else {
        showHorizon.value = true;
    }
};

const toggleDragTimeMode = () => {
    if (!isHorizonFixed.value) {
        setSkyFrameMode('horizon');
        dragTimeMode.value = true;
        return;
    }
    dragTimeMode.value = !dragTimeMode.value;
};

const handleConsoleTimeUpdate = (payload) => {
    if (!payload) return;
    if (payload.isLive) {
        const now = Number.isFinite(payload.timeMs) ? payload.timeMs : Date.now();
        liveNowMs.value = now;
        isLiveTime.value = true;
        emit('update-obs-time', { isLive: true, timeMs: now });
    } else if (Number.isFinite(payload.timeMs)) {
        isLiveTime.value = false;
        scrubTimeMs.value = payload.timeMs;
        emit('update-obs-time', { isLive: false, timeMs: payload.timeMs });
    } else {
        return;
    }
    refreshReadoutAfterTimeChange();
    scheduleDraw();
};

// --- 60 FPS STEREOGRAPHIC ALTITUDE CIRCLE & LOCAL HORIZON RENDERER ---
const drawAltitudeCurveAndGround = (
    ctx,
    cam,
    horiz,
    w,
    h,
    altDeg,
    fillBelowStyle,
    strokeStyle,
    lineWidth = 1.5,
    lineDash = null
) => {
    const zC = cam.Cx * horiz.Zx + cam.Cy * horiz.Zy + cam.Cz * horiz.Zz;
    const zE = cam.Ex * horiz.Zx + cam.Ey * horiz.Zy + cam.Ez * horiz.Zz;
    const zN = cam.Nx * horiz.Zx + cam.Ny * horiz.Zy + cam.Nz * horiz.Zz;

    const altRad = altDeg * DEG2RAD;
    const s = Math.sin(altRad);
    const cosAlt = Math.cos(altRad);
    const a = zC + s;
    const diag = Math.hypot(w, h);
    const rThresh = Math.max(8000, diag * 4.0);

    // Branch 1: Moderate stereographic circle radius (exact Euclidean circle on screen)
    if (Math.abs(a) * rThresh >= 2.0 * cam.f * cosAlt) {
        const invA = 1.0 / a;
        const cx = cam.halfW - cam.f * 2.0 * zE * invA;
        const cy = cam.halfH - cam.f * 2.0 * zN * invA;
        const R = Math.abs(cam.f * 2.0 * cosAlt * invA);

        const nearX = Math.max(0, Math.min(w, cx));
        const nearY = Math.max(0, Math.min(h, cy));
        const minDist = Math.hypot(cx - nearX, cy - nearY);
        const farX = cx < cam.halfW ? w : 0;
        const farY = cy < cam.halfH ? h : 0;
        const maxDist = Math.hypot(cx - farX, cy - farY);

        if (fillBelowStyle) {
            ctx.fillStyle = fillBelowStyle;
            if (a > 0) {
                // Sky above altDeg is INSIDE circle; ground (Alt < altDeg) is OUTSIDE
                if (minDist >= R) {
                    ctx.fillRect(0, 0, w, h);
                } else if (maxDist > R) {
                    ctx.beginPath();
                    ctx.rect(-2, -2, w + 4, h + 4);
                    ctx.arc(cx, cy, R, 0, TAU);
                    ctx.fill('evenodd');
                }
            } else {
                // Ground (Alt < altDeg) is INSIDE circle
                if (maxDist <= R) {
                    ctx.fillRect(0, 0, w, h);
                } else if (minDist < R) {
                    ctx.beginPath();
                    ctx.arc(cx, cy, R, 0, TAU);
                    ctx.fill();
                }
            }
        }

        if (strokeStyle && minDist <= R + 4 && maxDist >= R - 4) {
            ctx.save();
            if (lineDash) ctx.setLineDash(lineDash);
            ctx.strokeStyle = strokeStyle;
            ctx.lineWidth = lineWidth;
            ctx.beginPath();
            ctx.arc(cx, cy, R, 0, TAU);
            ctx.stroke();
            ctx.restore();
        }
        return;
    }

    // Branch 2: Large radius / straight-line regime (exact rationalized quadratic, zero float32 arc jitter)
    const G = Math.hypot(zE, zN);
    const centerG = 4.0 * cam.f * (s - zC);
    if (G < 1e-5) {
        if (fillBelowStyle && centerG > 0) {
            ctx.fillStyle = fillBelowStyle;
            ctx.fillRect(0, 0, w, h);
        }
        return;
    }

    const ux = zE / G;
    const uy = zN / G;
    const tx = -uy;
    const ty = ux;
    const D = diag * 0.65;
    const halfSpanN = 0.5 * (Math.abs(ux) * w + Math.abs(uy) * h) + 8;
    const A = a / cam.f;
    const B = 4.0 * G;
    const STEPS = HORIZ_STEPS;

    let minN = Infinity;
    let maxN = -Infinity;
    const ptsX = horizPtsX;
    const ptsY = horizPtsY;

    for (let i = 0; i <= STEPS; i++) {
        const t = -D + (2.0 * D * i) / STEPS;
        const C = A * t * t + centerG;
        const disc = B * B - 4.0 * A * C;
        if (disc < 0) {
            if (fillBelowStyle && centerG > 0) {
                ctx.fillStyle = fillBelowStyle;
                ctx.fillRect(0, 0, w, h);
            }
            return;
        }
        const n = (-2.0 * C) / (B + Math.sqrt(disc));
        if (n < minN) minN = n;
        if (n > maxN) maxN = n;
        ptsX[i] = cam.halfW + t * tx + n * ux;
        ptsY[i] = cam.halfH + t * ty + n * uy;
    }

    if (minN > halfSpanN || maxN < -halfSpanN) {
        if (fillBelowStyle && centerG > 0) {
            ctx.fillStyle = fillBelowStyle;
            ctx.fillRect(0, 0, w, h);
        }
        return;
    }

    if (fillBelowStyle) {
        const farN = diag * 1.25;
        ctx.fillStyle = fillBelowStyle;
        ctx.beginPath();
        ctx.moveTo(ptsX[0], ptsY[0]);
        for (let i = 1; i <= STEPS; i++) {
            ctx.lineTo(ptsX[i], ptsY[i]);
        }
        ctx.lineTo(cam.halfW + D * tx + farN * ux, cam.halfH + D * ty + farN * uy);
        ctx.lineTo(cam.halfW - D * tx + farN * ux, cam.halfH - D * ty + farN * uy);
        ctx.closePath();
        ctx.fill();
    }

    if (strokeStyle) {
        ctx.save();
        if (lineDash) ctx.setLineDash(lineDash);
        ctx.strokeStyle = strokeStyle;
        ctx.lineWidth = lineWidth;
        ctx.beginPath();
        ctx.moveTo(ptsX[0], ptsY[0]);
        for (let i = 1; i <= STEPS; i++) {
            ctx.lineTo(ptsX[i], ptsY[i]);
        }
        ctx.stroke();
        ctx.restore();
    }
};

const drawHorizonOverlay = (ctx, cam, w, h, fov, minPointCos, overlapsPlaced, precomputedHoriz = null) => {
    const loc = observerLocation.value;
    if (!loc && !precomputedHoriz) return;

    const horiz = precomputedHoriz || makeHorizontalBasis(loc.lat, loc.lon, effectiveTimeMs.value);

    // 1. Shaded below-horizon ground veil + soft horizon glow + crisp Alt = 0° curve
    drawAltitudeCurveAndGround(
        ctx,
        cam,
        horiz,
        w,
        h,
        0.0,
        'rgba(5, 9, 15, 0.74)',
        'rgba(45, 212, 191, 0.22)',
        4.2,
        null
    );
    drawAltitudeCurveAndGround(
        ctx,
        cam,
        horiz,
        w,
        h,
        0.0,
        null,
        'rgba(52, 211, 153, 0.82)',
        1.6,
        null
    );

    // 2. Minimum Imaging Altitude dashed ring (matching AltitudeGraph yellow threshold)
    const minAlt = minAltitudeDeg.value;
    if (minAlt > 0.5 && minAlt < 88) {
        drawAltitudeCurveAndGround(
            ctx,
            cam,
            horiz,
            w,
            h,
            minAlt,
            null,
            'rgba(234, 179, 8, 0.44)',
            1.25,
            [6, 5]
        );

        // Place a concise "Min XX°" tag near the view center's azimuth along the min-altitude curve
        const eastC = cam.Cx * horiz.Ehx + cam.Cy * horiz.Ehy;
        const northC = cam.Cx * horiz.Nhx + cam.Cy * horiz.Nhy + cam.Cz * horiz.Nhz;
        const azLen = Math.hypot(eastC, northC);
        const cosAz = azLen > 1e-5 ? northC / azLen : 0.0;
        const sinAz = azLen > 1e-5 ? eastC / azLen : 1.0;
        const sinMin = Math.sin(minAlt * DEG2RAD);
        const cosMin = Math.cos(minAlt * DEG2RAD);
        const mx3 = sinMin * horiz.Zx + cosMin * (cosAz * horiz.Nhx + sinAz * horiz.Ehx);
        const my3 = sinMin * horiz.Zy + cosMin * (cosAz * horiz.Nhy + sinAz * horiz.Ehy);
        const mz3 = sinMin * horiz.Zz + cosMin * (cosAz * horiz.Nhz);
        const mCosC = mx3 * cam.Cx + my3 * cam.Cy + mz3 * cam.Cz;

        if (mCosC > minPointCos) {
            const mFk = (cam.f * 2.0) / (1.0 + mCosC);
            const msx = cam.halfW - mFk * (mx3 * cam.Ex + my3 * cam.Ey + mz3 * cam.Ez);
            const msy = cam.halfH - mFk * (mx3 * cam.Nx + my3 * cam.Ny + mz3 * cam.Nz);
            if (msx > 46 && msx < w - 46 && msy > 34 && msy < h - 42) {
                const tag = `Min ${Math.round(minAlt)}°`;
                ctx.font = '500 10px system-ui, -apple-system, sans-serif';
                const tw = getMeasuredTextWidth(ctx, 'minalt', tag);
                const pw = tw + 10;
                const ph = 16;
                const x1 = msx - pw * 0.5;
                const y1 = msy - ph * 0.5;
                if (!overlapsPlaced(x1, y1, x1 + pw, y1 + ph, 3)) {
                    pushPlacedBox(x1, y1, x1 + pw, y1 + ph);
                    ctx.fillStyle = 'rgba(10, 14, 22, 0.88)';
                    ctx.strokeStyle = 'rgba(234, 179, 8, 0.45)';
                    ctx.lineWidth = 1;
                    ctx.beginPath();
                    if (ctx.roundRect) {
                        ctx.roundRect(x1, y1, pw, ph, 4);
                    } else {
                        ctx.rect(x1, y1, pw, ph);
                    }
                    ctx.fill();
                    ctx.stroke();

                    ctx.fillStyle = 'rgba(253, 224, 71, 0.9)';
                    ctx.textAlign = 'center';
                    ctx.textBaseline = 'middle';
                    ctx.fillText(tag, msx, msy + 0.5);
                }
            }
        }
    }

    // 3. Cardinal & Intercardinal Compass Badges along Alt = 0°
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';

    for (let i = 0; i < COMPASS_POINTS.length; i++) {
        const cp = COMPASS_POINTS[i];
        if (!cp.major && fov > 85) continue;

        const px3 = cp.cosAz * horiz.Nhx + cp.sinAz * horiz.Ehx;
        const py3 = cp.cosAz * horiz.Nhy + cp.sinAz * horiz.Ehy;
        const pz3 = cp.cosAz * horiz.Nhz;
        const cosC = px3 * cam.Cx + py3 * cam.Cy + pz3 * cam.Cz;
        if (cosC < minPointCos) continue;

        const fk = (cam.f * 2.0) / (1.0 + cosC);
        const sx = cam.halfW - fk * (px3 * cam.Ex + py3 * cam.Ey + pz3 * cam.Ez);
        const sy = cam.halfH - fk * (px3 * cam.Nx + py3 * cam.Ny + pz3 * cam.Nz);
        if (sx < 20 || sx > w - 20 || sy < 24 || sy > h - 34) continue;

        ctx.font = cp.major
            ? '700 11.5px system-ui, -apple-system, sans-serif'
            : '600 10px system-ui, -apple-system, sans-serif';
        const tw = getMeasuredTextWidth(ctx, cp.major ? 'cmpM' : 'cmpS', cp.label);
        const pw = Math.max(cp.major ? 22 : 20, tw + (cp.major ? 12 : 9));
        const ph = cp.major ? 20 : 17;
        const x1 = sx - pw * 0.5;
        const y1 = sy - ph * 0.5;

        pushPlacedBox(x1, y1, x1 + pw, y1 + ph);

        ctx.fillStyle = 'rgba(7, 13, 20, 0.92)';
        ctx.strokeStyle =
            cp.label === 'N'
                ? 'rgba(248, 113, 113, 0.72)'
                : cp.major
                  ? 'rgba(52, 211, 153, 0.72)'
                  : 'rgba(52, 211, 153, 0.38)';
        ctx.lineWidth = cp.major ? 1.25 : 1.0;
        ctx.beginPath();
        if (ctx.roundRect) {
            ctx.roundRect(x1, y1, pw, ph, 5);
        } else {
            ctx.rect(x1, y1, pw, ph);
        }
        ctx.fill();
        ctx.stroke();

        ctx.fillStyle =
            cp.label === 'N' ? '#f87171' : cp.major ? '#34d399' : 'rgba(52, 211, 153, 0.88)';
        ctx.fillText(cp.label, sx, sy + 0.5);
    }

    // 4. Overhead Zenith Marker (+ Zenith)
    const zCosC = horiz.Zx * cam.Cx + horiz.Zy * cam.Cy + horiz.Zz * cam.Cz;
    if (zCosC > minPointCos) {
        const zFk = (cam.f * 2.0) / (1.0 + zCosC);
        const zsx = cam.halfW - zFk * (horiz.Zx * cam.Ex + horiz.Zy * cam.Ey + horiz.Zz * cam.Ez);
        const zsy = cam.halfH - zFk * (horiz.Zx * cam.Nx + horiz.Zy * cam.Ny + horiz.Zz * cam.Nz);
        if (zsx > 36 && zsx < w - 36 && zsy > 30 && zsy < h - 36) {
            ctx.strokeStyle = 'rgba(52, 211, 153, 0.78)';
            ctx.lineWidth = 1.2;
            ctx.beginPath();
            ctx.moveTo(zsx - 5, zsy);
            ctx.lineTo(zsx + 5, zsy);
            ctx.moveTo(zsx, zsy - 5);
            ctx.lineTo(zsx, zsy + 5);
            ctx.stroke();

            ctx.font = '600 10px system-ui, -apple-system, sans-serif';
            ctx.textAlign = 'left';
            ctx.textBaseline = 'middle';
            ctx.lineJoin = 'round';
            ctx.miterLimit = 2;
            ctx.lineWidth = 3.0;
            ctx.strokeStyle = 'rgba(3, 6, 12, 0.92)';
            ctx.strokeText('Zenith', zsx + 8, zsy);
            ctx.fillStyle = '#34d399';
            ctx.fillText('Zenith', zsx + 8, zsy);
        }
    }
};

// --- PRE-INDEXED NON-REACTIVE SCENE TABLES + 3D SPHERICAL PATCH SOA ---
let skyPhotosList = [];
let photoSoA = new Float32Array(0);      // [x, y, z, fovRad] * N
let photoPatchIds = new Uint8Array(0);   // patchId (0..47) * N
let photoIndexByName = new Map();
let photoByNormName = new Map();
let photoByKey = new Map();

let preparedDsos = [];
let dsoSoA = new Float32Array(0);        // [x, y, z, majHalfRad] * M
let dsoPatchIds = new Uint8Array(0);     // patchId (0..47) * M
let dsoByNameMap = new Map();
let objectByNameMap = new Map();

const visibleSectorPool = [];
let visibleSectorCount = 0;
const pushVisibleSector = (sec, img, ux, uy, vx, vy, cx, cy) => {
    let slot = visibleSectorPool[visibleSectorCount];
    if (!slot) {
        slot = { sec, img, ux, uy, vx, vy, cx, cy };
        visibleSectorPool[visibleSectorCount] = slot;
    } else {
        slot.sec = sec;
        slot.img = img;
        slot.ux = ux;
        slot.uy = uy;
        slot.vx = vx;
        slot.vy = vy;
        slot.cx = cx;
        slot.cy = cy;
    }
    visibleSectorCount++;
};

const normalizePhotoKeyName = (name) =>
    String(name || '')
        .trim()
        .toUpperCase()
        .replace(/[\s_-]+/g, '');

const normCatalogKey = (s) => (s ? String(s).toLowerCase().replace(/\s+/g, '') : '');

const lookupCatalogObject = (name) => {
    if (!name) return null;
    return objectByNameMap.get(name) || objectByNameMap.get(normCatalogKey(name)) || null;
};

const rebuildPhotoSoA = () => {
    const n = skyPhotosList.length;
    const soa = new Float32Array(n * 4);
    const patches = new Uint8Array(n);
    const idxMap = new Map();
    for (let i = 0; i < n; i++) {
        const p = skyPhotosList[i];
        const o = i * 4;
        soa[o] = p.x;
        soa[o + 1] = p.y;
        soa[o + 2] = p.z;
        soa[o + 3] = p.fov * DEG2RAD;
        patches[i] = getSkyPatchId(p.ra, p.dec);
        if (p.name && !idxMap.has(p.name)) {
            idxMap.set(p.name, i);
        }
    }
    photoSoA = soa;
    photoPatchIds = patches;
    photoIndexByName = idxMap;
    skyPhotoCount.value = n;
};

const rebuildSceneIndex = () => {
    // Step 1: Build O(1) catalog lookup map first so photos pre-link refObj once
    const objs = props.objects;
    const byName = new Map();
    if (Array.isArray(objs)) {
        for (let i = 0; i < objs.length; i++) {
            const obj = objs[i];
            if (!obj || !obj.name) continue;
            byName.set(obj.name, obj);
            const normName = normCatalogKey(obj.name);
            if (normName && !byName.has(normName)) {
                byName.set(normName, obj);
            }
            if (obj.aliases) {
                const parts = String(obj.aliases).split(',');
                for (let p = 0; p < parts.length; p++) {
                    const nk = normCatalogKey(parts[p]);
                    if (nk && !byName.has(nk)) {
                        byName.set(nk, obj);
                    }
                }
            }
        }
    }
    objectByNameMap = byName;

    // Step 2: Build unified Sky Photos list.
    // When v2 pre-composited spherical Sky Atlas sectors are active, skip indexing all 13k
    // individual overlapping target tiles; only retain the currently selected active target's
    // high-res cutout for ultra-close-up (< ~6° FOV) inlay.
    const byKey = new Map();
    const byNorm = new Map();
    const cachedNames = new Set();

    const mergeLivePhoto = (name, commonName, ra, dec, fov, url, isCustom) => {
        if (name) cachedNames.add(name);
        const norm = !isCustom ? normalizePhotoKeyName(name) : '';
        if (norm) {
            const existing = byNorm.get(norm);
            if (
                existing &&
                Math.abs(existing.ra - ra) < 0.05 &&
                Math.abs(existing.dec - dec) < 0.05 &&
                Math.abs(existing.fov - fov) < 0.3
            ) {
                existing.url = url;
                if (!existing.refObj && name) {
                    existing.refObj = lookupCatalogObject(name);
                }
                return;
            }
        }

        const key = `${ra.toFixed(3)}_${dec.toFixed(3)}_${fov.toFixed(2)}`;
        const prev = byKey.get(key);
        if (prev) {
            prev.url = url;
            if (!prev.refObj && name) {
                prev.refObj = lookupCatalogObject(name);
            }
            return;
        }

        const a = ra * DEG2RAD;
        const d = dec * DEG2RAD;
        const cd = Math.cos(d);
        const entry = {
            name,
            commonName: commonName || '',
            ra,
            dec,
            x: cd * Math.cos(a),
            y: cd * Math.sin(a),
            z: Math.sin(d),
            fov,
            url,
            atlasTile: null,
            refObj: lookupCatalogObject(name),
            isCustom: Boolean(isCustom)
        };
        byKey.set(key, entry);
        if (norm && !byNorm.has(norm)) {
            byNorm.set(norm, entry);
        }
    };

    if (!skyAtlasSectorMode.value) {
        const atlasList = skyAtlasItems.value;
        for (let i = 0; i < atlasList.length; i++) {
            const at = atlasList[i];
            const key = `${at.ra.toFixed(3)}_${at.dec.toFixed(3)}_${at.fov.toFixed(2)}`;
            const norm = at.normName || normalizePhotoKeyName(at.name);
            const entry = {
                name: at.name,
                commonName: at.commonName || '',
                ra: at.ra,
                dec: at.dec,
                x: at.x,
                y: at.y,
                z: at.z,
                fov: at.fov,
                url: null,
                atlasTile: at,
                refObj: lookupCatalogObject(at.name),
                isCustom: Boolean(at.isCustom)
            };
            byKey.set(key, entry);
            if (at.name) cachedNames.add(at.name);
            if (norm && !byNorm.has(norm)) {
                byNorm.set(norm, entry);
            }
        }

        const serverImgs = cachedServerImages.value;
        for (let i = 0; i < serverImgs.length; i++) {
            const img = serverImgs[i];
            if (!img?.url || img.ra === undefined || img.dec === undefined) continue;
            const cleanUrl = String(img.url).split('?')[0];
            const ra = Number(img.ra);
            const dec = Number(img.dec);
            const fov = Math.max(Number(img.fov) || 1.0, 0.2);
            if (!Number.isFinite(ra) || !Number.isFinite(dec)) continue;
            mergeLivePhoto(
                img.name,
                img.common_name || '',
                ra,
                dec,
                fov,
                cleanUrl,
                Boolean(img.is_custom)
            );
        }

        if (Array.isArray(objs)) {
            for (let i = 0; i < objs.length; i++) {
                const o = objs[i];
                if (!o || o.status !== 'cached' || !o.image_url) continue;
                const ra = Number(o.center_ra !== undefined ? o.center_ra : o.center_ra ?? o.ra);
                const dec = Number(o.center_dec !== undefined ? o.center_dec : o.dec);
                const fov = Math.max(Number(o.image_fov || props.baseImageFov || 1.0), 0.2);
                if (!Number.isFinite(ra) || !Number.isFinite(dec)) continue;
                mergeLivePhoto(
                    o.name,
                    o.common_name && o.common_name !== 'N/A' ? o.common_name : '',
                    ra,
                    dec,
                    fov,
                    String(o.image_url),
                    Boolean(o.custom_framed)
                );
            }
        }
    }

    if (props.object?.image_url) {
        const ra = Number(
            props.object.center_ra !== undefined ? props.object.center_ra : props.object.ra
        );
        const dec = Number(
            props.object.center_dec !== undefined ? props.object.center_dec : props.object.dec
        );
        const fov = Math.max(Number(props.object.image_fov || props.baseImageFov || 1.0), 0.2);
        if (Number.isFinite(ra) && Number.isFinite(dec)) {
            mergeLivePhoto(
                props.object.name,
                props.object.common_name && props.object.common_name !== 'N/A'
                    ? props.object.common_name
                    : '',
                ra,
                dec,
                fov,
                String(props.object.image_url),
                Boolean(props.object.custom_framed)
            );
        }
    }

    skyPhotosList = Array.from(byKey.values()).sort((a, b) => b.fov - a.fov);
    photoByKey = byKey;
    photoByNormName = byNorm;
    rebuildPhotoSoA();

    // Step 3: Build pre-sorted astronomical DSO table + contiguous SoA & patch IDs (FOV-independent)
    const dsoList = [];
    const dsoMap = new Map();
    if (Array.isArray(objs)) {
        for (let i = 0; i < objs.length; i++) {
            const obj = objs[i];
            if (!obj || !obj.name) continue;
            const ra = parseFloat(obj.ra);
            const dec = parseFloat(obj.dec);
            if (!Number.isFinite(ra) || !Number.isFinite(dec)) continue;

            const a = ra * DEG2RAD;
            const d = dec * DEG2RAD;
            const cd = Math.cos(d);
            const isMessier = /^M\s*\d+$/i.test(obj.name);
            const shortName = isMessier ? obj.name.replace(/\s+/g, '') : obj.name;
            const commonName =
                obj.common_name && obj.common_name !== 'N/A' ? String(obj.common_name).trim() : '';
            const hasCommon = commonName.length > 0;
            const zoomLabel =
                hasCommon && commonName.length <= 26 ? `${shortName} · ${commonName}` : shortName;
            const mag = typeof obj.mag === 'number' && obj.mag < 90 ? obj.mag : 13.5;
            const maj = typeof obj.maj_ax === 'number' ? obj.maj_ax : 0;

            const entry = {
                obj,
                name: obj.name,
                shortName,
                commonName,
                zoomLabel,
                hasCommon,
                ra,
                dec,
                x: cd * Math.cos(a),
                y: cd * Math.sin(a),
                z: Math.sin(d),
                isMessier,
                mag,
                maj,
                majHalfRad: (maj / 60.0) * DEG2RAD * 0.5,
                score:
                    (isMessier ? 120 : 0) +
                    (hasCommon ? 45 : 0) +
                    Math.min(maj, 60) -
                    mag * 3.2
            };
            dsoList.push(entry);
            dsoMap.set(obj.name, entry);
        }
    }

    dsoList.sort((a, b) => b.score - a.score);
    const m = dsoList.length;
    const dSoa = new Float32Array(m * 4);
    const dPatches = new Uint8Array(m);
    for (let i = 0; i < m; i++) {
        const d = dsoList[i];
        const o = i * 4;
        dSoa[o] = d.x;
        dSoa[o + 1] = d.y;
        dSoa[o + 2] = d.z;
        dSoa[o + 3] = d.majHalfRad;
        dPatches[i] = getSkyPatchId(d.ra, d.dec);
    }

    preparedDsos = dsoList;
    dsoSoA = dSoa;
    dsoPatchIds = dPatches;
    dsoByNameMap = dsoMap;
};

// O(1) incremental live-photo upsert (avoids re-fetching /api/cache/sky-images or rebuilding 7,300 items)
const upsertLivePhotoFromObject = (obj) => {
    if (!obj || !obj.image_url) return;
    if (skyAtlasSectorMode.value) {
        if (obj.name !== props.object?.name) return;
        photoByKey.clear();
        photoByNormName.clear();
        skyPhotosList = [];
    }
    const ra = Number(obj.center_ra !== undefined ? obj.center_ra : obj.ra);
    const dec = Number(obj.center_dec !== undefined ? obj.center_dec : obj.dec);
    const fov = Math.max(Number(obj.image_fov || props.baseImageFov || 1.0), 0.2);
    if (!Number.isFinite(ra) || !Number.isFinite(dec)) return;

    const url = String(obj.image_url);
    const isCustom = Boolean(obj.custom_framed);
    const norm = !isCustom ? normalizePhotoKeyName(obj.name) : '';

    if (obj.name) {
        const dsoEntry = dsoByNameMap.get(obj.name);
        if (dsoEntry) dsoEntry.isCached = true;
    }

    if (norm) {
        const existing = photoByNormName.get(norm);
        if (
            existing &&
            Math.abs(existing.ra - ra) < 0.05 &&
            Math.abs(existing.dec - dec) < 0.05 &&
            Math.abs(existing.fov - fov) < 0.3
        ) {
            existing.url = url;
            if (!existing.refObj) existing.refObj = obj;
            return;
        }
    }

    const key = `${ra.toFixed(3)}_${dec.toFixed(3)}_${fov.toFixed(2)}`;
    const prev = photoByKey.get(key);
    if (prev) {
        prev.url = url;
        if (!prev.refObj) prev.refObj = obj;
        return;
    }

    const a = ra * DEG2RAD;
    const d = dec * DEG2RAD;
    const cd = Math.cos(d);
    const entry = {
        name: obj.name,
        commonName: obj.common_name && obj.common_name !== 'N/A' ? obj.common_name : '',
        ra,
        dec,
        x: cd * Math.cos(a),
        y: cd * Math.sin(a),
        z: Math.sin(d),
        fov,
        url,
        atlasTile: null,
        refObj: obj,
        isCustom
    };
    photoByKey.set(key, entry);
    if (norm && !photoByNormName.has(norm)) {
        photoByNormName.set(norm, entry);
    }

    // Insert in descending FOV order
    let insertAt = skyPhotosList.length;
    for (let i = 0; i < skyPhotosList.length; i++) {
        if (fov >= skyPhotosList[i].fov) {
            insertAt = i;
            break;
        }
    }
    skyPhotosList.splice(insertAt, 0, entry);
    rebuildPhotoSoA();
};

const fetchCachedSkyImages = async () => {
    try {
        const res = await fetch('/api/cache/sky-images');
        if (!res.ok) return;
        const data = await res.json();
        if (Array.isArray(data?.images)) {
            cachedServerImages.value = data.images;
            rebuildSceneIndex();
            scheduleDraw();
        }
    } catch (err) {
        console.warn('Could not load cached sky images:', err);
    }
};

const ensureImageLoaded = (url, allowQueue = true) => {
    if (!url) return null;
    const existing = imageElementCache.get(url);
    if (existing) {
        return existing.status === 'ready' ? existing.img : null;
    }
    if (!allowQueue || inflightImageLoads >= MAX_CONCURRENT_INDIVIDUAL_LOADS) {
        return null;
    }
    const img = new Image();
    img.decoding = 'async';
    const record = { img, status: 'loading' };
    imageElementCache.set(url, record);
    inflightImageLoads++;
    img.onload = () => {
        inflightImageLoads = Math.max(0, inflightImageLoads - 1);
        record.status = 'ready';
        scheduleDraw();
    };
    img.onerror = () => {
        inflightImageLoads = Math.max(0, inflightImageLoads - 1);
        record.status = 'error';
    };
    img.src = url;
    return null;
};

const isOffTarget = computed(() => {
    const tgt = getCatalogTargetCoords();
    const ctr = getCurrentCenterRaDec();
    return angularDistanceDeg(ctr.ra, ctr.dec, tgt.ra, tgt.dec) > 0.35;
});

// --- FAST 3D POLYLINE HELPER (Zero trig, zero allocations) ---
const traceXYZPolyline = (ctx, xyz, count, cam, maxJumpSq) => {
    let started = false;
    let prevX = 0;
    let prevY = 0;

    for (let i = 0; i < count; i++) {
        const o = i * 3;
        const x = xyz[o];
        const y = xyz[o + 1];
        const z = xyz[o + 2];
        const cosC = x * cam.Cx + y * cam.Cy + z * cam.Cz;
        if (cosC <= 0.02) {
            started = false;
            continue;
        }
        const fk = (cam.f * 2.0) / (1.0 + cosC);
        const sx = cam.halfW - fk * (x * cam.Ex + y * cam.Ey + z * cam.Ez);
        const sy = cam.halfH - fk * (x * cam.Nx + y * cam.Ny + z * cam.Nz);

        if (!started) {
            ctx.moveTo(sx, sy);
            started = true;
        } else {
            const dx = sx - prevX;
            const dy = sy - prevY;
            if (dx * dx + dy * dy > maxJumpSq) {
                ctx.moveTo(sx, sy);
            } else {
                ctx.lineTo(sx, sy);
            }
        }
        prevX = sx;
        prevY = sy;
    }
};

// --- CANVAS RENDERING ---
const scheduleDraw = () => {
    if (rafId !== null) return;
    rafId = requestAnimationFrame(() => {
        rafId = null;
        drawSkyCanvas();
    });
};

const drawSkyCanvas = () => {
    const canvas = canvasRef.value;
    if (!canvas) return;
    const w = viewportW.value;
    const h = viewportH.value;
    if (w <= 2 || h <= 2) return;

    ensureSprites();

    // Cap HiDPI multiplier slightly on ultra-high-resolution viewports to save fillrate
    const rawDpr = window.devicePixelRatio || 1;
    const maxDpr = w * h > 1650000 ? 1.5 : 2.0;
    const dpr = Math.min(rawDpr, maxDpr);
    const targetW = Math.round(w * dpr);
    const targetH = Math.round(h * dpr);
    if (canvas.width !== targetW || canvas.height !== targetH) {
        canvas.width = targetW;
        canvas.height = targetH;
        canvasCtx = null;
        cachedBgGrad = null;
    }

    if (!canvasCtx) {
        canvasCtx = canvas.getContext('2d', { alpha: false });
    }
    const ctx = canvasCtx;
    if (!ctx) return;

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    // 1. Cached deep night sky background gradient
    const bgKey = `${w}x${h}`;
    if (!cachedBgGrad || cachedBgKey !== bgKey) {
        const bgGrad = ctx.createRadialGradient(
            w * 0.5,
            h * 0.5,
            Math.min(w, h) * 0.08,
            w * 0.5,
            h * 0.5,
            Math.max(w, h) * 0.8
        );
        bgGrad.addColorStop(0, '#090d18');
        bgGrad.addColorStop(0.65, '#050811');
        bgGrad.addColorStop(1, '#03050a');
        cachedBgGrad = bgGrad;
        cachedBgKey = bgKey;
    }
    ctx.fillStyle = cachedBgGrad;
    ctx.fillRect(0, 0, w, h);

    const fov = mapFovDeg.value;
    const loc = observerLocation.value;
    const horizBasis =
        loc && (isHorizonFixed.value || showHorizon.value)
            ? makeHorizontalBasis(loc.lat, loc.lon, effectiveTimeMs.value)
            : null;

    let cam;
    let centerRa;
    let centerDec;
    if (isHorizonFixed.value && horizBasis) {
        cam = makeAltAzCameraBasis(viewAz.value, viewAlt.value, horizBasis, w, h, fov);
        centerRa = wrapRaDeg(Math.atan2(cam.Cy, cam.Cx) * RAD2DEG);
        centerDec = clampDecDeg(Math.asin(Math.max(-1.0, Math.min(1.0, cam.Cz))) * RAD2DEG);
    } else {
        centerRa = viewRa.value;
        centerDec = viewDec.value;
        cam = makeCameraBasis(centerRa, centerDec, w, h, fov);
    }

    // Compute exact angular corner radius for tight cone culling
    const cornerRho = Math.hypot(w * 0.5, h * 0.5) / cam.f;
    const maxVisRad = Math.min(Math.PI * 0.68, 2.0 * Math.atan(cornerRho * 0.5) + 0.14);
    const minPointCos = Math.cos(maxVisRad);
    const cosVis = minPointCos;
    const sinVis = Math.sin(maxVisRad);
    const maxJumpPx = Math.max(w, h) * 1.4;
    const maxJumpSq = maxJumpPx * maxJumpPx;

    // 1b. Update 48-patch spherical bounding-cone visibility mask once per frame
    const coneLimitRad = Math.min(Math.PI, maxVisRad + 0.16);
    const cosCone = Math.cos(coneLimitRad);
    const sinCone = Math.sin(coneLimitRad);
    for (let p = 0; p < PATCH_COUNT; p++) {
        if (coneLimitRad + patchRadius[p] >= Math.PI) {
            visPatch[p] = 1;
        } else {
            const thresh = cosCone * patchCosR[p] - sinCone * patchSinR[p];
            visPatch[p] =
                cam.Cx * patchCx[p] + cam.Cy * patchCy[p] + cam.Cz * patchCz[p] >= thresh ? 1 : 0;
        }
    }

    const buildHitItems = !isPanning.value;
    interactiveCount = 0;
    placedBoxesCount = 0;

    const overlapsPlaced = (x1, y1, x2, y2, pad = 2) => {
        for (let i = 0; i < placedBoxesCount; i++) {
            const b = placedBoxesPool[i];
            if (x1 - pad < b.x2 && x2 + pad > b.x1 && y1 - pad < b.y2 && y2 + pad > b.y1) {
                return true;
            }
        }
        return false;
    };

    // 2. Milky Way Diffuse Cloud Sprites (~25 drawImage calls, < 0.05 ms)
    if (preparedSky && mwCloudSprite && fov >= 3.5) {
        const mwNodes = preparedSky.mwNodes;
        const fovFade = fov < 8 ? (fov - 3.5) / 4.5 : 1.0;
        const mwMinCos = Math.cos(Math.min(Math.PI * 0.78, maxVisRad + 0.32));
        ctx.save();
        for (let i = 0; i < mwNodes.length; i += 5) {
            const x = mwNodes[i];
            const y = mwNodes[i + 1];
            const z = mwNodes[i + 2];
            const cosC = x * cam.Cx + y * cam.Cy + z * cam.Cz;
            if (cosC < mwMinCos) continue;

            const fk = (cam.f * 2.0) / (1.0 + cosC);
            const sx = cam.halfW - fk * (x * cam.Ex + y * cam.Ey + z * cam.Ez);
            const sy = cam.halfH - fk * (x * cam.Nx + y * cam.Ny + z * cam.Nz);
            const rPx = mwNodes[i + 3] * fk;
            if (sx + rPx < 0 || sx - rPx > w || sy + rPx < 0 || sy - rPx > h) continue;

            ctx.globalAlpha = mwNodes[i + 4] * fovFade;
            ctx.drawImage(mwCloudSprite, sx - rPx, sy - rPx, rPx * 2.0, rPx * 2.0);
        }
        ctx.restore();
    }

    // 2.5 Pre-Composited 3-Tier Multi-LOD Spherical Sky Survey Sector Map (48-Patch Culled, Zero Overdraw)
    if (showCachedPhotos.value && skyAtlasSectorMode.value) {
        const collectAndDrawSectors = (sectors, allowQueue, highPriority, drawDirectly) => {
            let anyMissingSheet = false;

            const processSector = (sec) => {
                if (maxVisRad + sec.radiusRad < Math.PI) {
                    const thresh = cosVis * sec.cosR - sinVis * sec.sinR;
                    if (cam.Cx * sec.tx + cam.Cy * sec.ty + cam.Cz * sec.tz < thresh) return;
                }

                const c0 = sec.tx * cam.Cx + sec.ty * cam.Cy + sec.tz * cam.Cz;
                const cL = sec.lx * cam.Cx + sec.ly * cam.Cy + sec.lz * cam.Cz;
                const cR = sec.rx * cam.Cx + sec.ry * cam.Cy + sec.rz * cam.Cz;
                const cT = sec.topX * cam.Cx + sec.topY * cam.Cy + sec.topZ * cam.Cz;
                const cB = sec.botX * cam.Cx + sec.botY * cam.Cy + sec.botZ * cam.Cz;
                if (c0 <= -0.75 || cL <= -0.75 || cR <= -0.75 || cT <= -0.75 || cB <= -0.75) {
                    return;
                }

                const fk0 = (cam.f * 2.0) / (1.0 + c0);
                const fkL = (cam.f * 2.0) / (1.0 + cL);
                const fkR = (cam.f * 2.0) / (1.0 + cR);
                const fkT = (cam.f * 2.0) / (1.0 + cT);
                const fkB = (cam.f * 2.0) / (1.0 + cB);

                const x0 = cam.halfW - fk0 * (sec.tx * cam.Ex + sec.ty * cam.Ey + sec.tz * cam.Ez);
                const y0 = cam.halfH - fk0 * (sec.tx * cam.Nx + sec.ty * cam.Ny + sec.tz * cam.Nz);
                const xL = cam.halfW - fkL * (sec.lx * cam.Ex + sec.ly * cam.Ey + sec.lz * cam.Ez);
                const yL = cam.halfH - fkL * (sec.lx * cam.Nx + sec.ly * cam.Ny + sec.lz * cam.Nz);
                const xR = cam.halfW - fkR * (sec.rx * cam.Ex + sec.ry * cam.Ey + sec.rz * cam.Ez);
                const yR = cam.halfH - fkR * (sec.rx * cam.Nx + sec.ry * cam.Ny + sec.rz * cam.Nz);
                const xT = cam.halfW - fkT * (sec.topX * cam.Ex + sec.topY * cam.Ey + sec.topZ * cam.Ez);
                const yT = cam.halfH - fkT * (sec.topX * cam.Nx + sec.topY * cam.Ny + sec.topZ * cam.Nz);
                const xB = cam.halfW - fkB * (sec.botX * cam.Ex + sec.botY * cam.Ey + sec.botZ * cam.Ez);
                const yB = cam.halfH - fkB * (sec.botX * cam.Nx + sec.botY * cam.Ny + sec.botZ * cam.Nz);

                const minX = Math.min(xL, xR, xT, xB);
                const maxX = Math.max(xL, xR, xT, xB);
                const minY = Math.min(yL, yR, yT, yB);
                const maxY = Math.max(yL, yR, yT, yB);
                if (maxX < -16 || minX > w + 16 || maxY < -16 || minY > h + 16) return;

                const sheetImg = ensureAtlasSheetImage(sec.s, allowQueue, highPriority);
                if (!sheetImg) {
                    anyMissingSheet = true;
                    return;
                }

                const ux = xR - xL;
                const uy = yR - yL;
                const vx = xB - xT;
                const vy = yB - yT;
                const cx = x0 + 0.22 * (xL + xR + xT + xB - 4.0 * x0);
                const cy = y0 + 0.22 * (yL + yR + yT + yB - 4.0 * y0);

                if (drawDirectly) {
                    ctx.setTransform(dpr * ux, dpr * uy, dpr * vx, dpr * vy, dpr * cx, dpr * cy);
                    ctx.drawImage(sheetImg, sec.u, sec.v, sec.w, sec.h, -0.5, -0.5, 1.0, 1.0);
                } else {
                    pushVisibleSector(sec, sheetImg, ux, uy, vx, vy, cx, cy);
                }
            };

            const byPatch = sectors.byPatch;
            if (byPatch && sectors.length > 300) {
                const secCone = Math.min(Math.PI, maxVisRad + (sectors.maxRadiusRad || 0.16));
                const cosSC = Math.cos(secCone);
                const sinSC = Math.sin(secCone);
                for (let p = 0; p < PATCH_COUNT; p++) {
                    if (secCone + patchRadius[p] < Math.PI) {
                        const thresh = cosSC * patchCosR[p] - sinSC * patchSinR[p];
                        if (cam.Cx * patchCx[p] + cam.Cy * patchCy[p] + cam.Cz * patchCz[p] < thresh) {
                            continue;
                        }
                    }
                    const bucket = byPatch[p];
                    const bLen = bucket.length;
                    for (let i = 0; i < bLen; i++) {
                        processSector(bucket[i]);
                    }
                }
            } else {
                const nSec = sectors.length;
                for (let i = 0; i < nSec; i++) {
                    processSector(sectors[i]);
                }
            }
            return anyMissingSheet;
        };

        const flushSectorRange = (startIdx, endIdx) => {
            for (let i = startIdx; i < endIdx; i++) {
                const s = visibleSectorPool[i];
                ctx.setTransform(
                    dpr * s.ux,
                    dpr * s.uy,
                    dpr * s.vx,
                    dpr * s.vy,
                    dpr * s.cx,
                    dpr * s.cy
                );
                ctx.drawImage(s.img, s.sec.u, s.sec.v, s.sec.w, s.sec.h, -0.5, -0.5, 1.0, 1.0);
            }
        };

        ctx.globalCompositeOperation = 'source-over';
        ctx.globalAlpha = 1.0;

        const lod0 = skyAtlasLod0Sectors.value;
        const lod1 = skyAtlasLod1Sectors.value;
        const lod2 = skyAtlasLod2Sectors.value;

        if (fov > 50.0 || (lod1.length === 0 && lod2.length === 0)) {
            collectAndDrawSectors(lod0, true, true, true);
        } else if (fov > 19.0 || lod2.length === 0) {
            visibleSectorCount = 0;
            const missingLod1 = collectAndDrawSectors(lod1, true, !isPanning.value, false);
            if (missingLod1 && lod0.length > 0) {
                collectAndDrawSectors(lod0, true, true, true);
            }
            flushSectorRange(0, visibleSectorCount);
        } else {
            visibleSectorCount = 0;
            const missingLod2 = collectAndDrawSectors(lod2, true, !isPanning.value, false);
            const lod2End = visibleSectorCount;
            if (missingLod2) {
                const lod1Start = visibleSectorCount;
                const missingLod1 =
                    lod1.length > 0 ? collectAndDrawSectors(lod1, false, false, false) : true;
                const lod1End = visibleSectorCount;
                if (missingLod1 && lod0.length > 0) {
                    collectAndDrawSectors(lod0, true, true, true);
                }
                flushSectorRange(lod1Start, lod1End);
            }
            flushSectorRange(0, lod2End);
        }

        // Optional close-up HD inlay for the single selected active target when zoomed deep in (< ~7° FOV)
        if (skyPhotosList.length > 0 && fov <= 10.0) {
            const photo = skyPhotosList[0];
            if (photo?.url) {
                const cosC = photo.x * cam.Cx + photo.y * cam.Cy + photo.z * cam.Cz;
                if (cosC > 0.2) {
                    const k = 2.0 / (1.0 + cosC);
                    const fk = cam.f * k;
                    const sizePx = photo.fov * DEG2RAD * fk;
                    if (sizePx >= 150) {
                        const u = photo.x * cam.Ex + photo.y * cam.Ey + photo.z * cam.Ez;
                        const v = photo.x * cam.Nx + photo.y * cam.Ny + photo.z * cam.Nz;
                        const px = cam.halfW - fk * u;
                        const py = cam.halfH - fk * v;
                        if (
                            px + sizePx >= -20 &&
                            px - sizePx <= w + 20 &&
                            py + sizePx >= -20 &&
                            py - sizePx <= h + 20
                        ) {
                            const hiResImg = ensureImageLoaded(photo.url, true);
                            if (hiResImg) {
                                const vx = -photo.x * photo.z;
                                const vy = -photo.y * photo.z;
                                const vz = 1.0 - photo.z * photo.z;
                                let cosA = 1.0;
                                let sinA = 0.0;
                                if (vz > 1e-8) {
                                    const vDotC = vx * cam.Cx + vy * cam.Cy + vz * cam.Cz;
                                    const vDotE = vx * cam.Ex + vy * cam.Ey + vz * cam.Ez;
                                    const vDotN = vx * cam.Nx + vy * cam.Ny + vz * cam.Nz;
                                    const halfKVC = 0.5 * k * vDotC;
                                    const dxN = halfKVC * u - vDotE;
                                    const dyN = halfKVC * v - vDotN;
                                    const len = Math.hypot(dxN, dyN);
                                    if (len > 1e-9) {
                                        cosA = -dyN / len;
                                        sinA = dxN / len;
                                    }
                                }
                                const half = sizePx * 0.5;
                                ctx.setTransform(
                                    dpr * cosA,
                                    dpr * sinA,
                                    -dpr * sinA,
                                    dpr * cosA,
                                    px * dpr,
                                    py * dpr
                                );
                                ctx.fillStyle = 'rgba(5, 9, 18, 0.9)';
                                ctx.fillRect(-half, -half, sizePx, sizePx);
                                ctx.globalCompositeOperation = 'screen';
                                ctx.globalAlpha = 0.92;
                                ctx.drawImage(hiResImg, -half, -half, sizePx, sizePx);
                                ctx.globalCompositeOperation = 'source-over';
                                ctx.globalAlpha = 1.0;
                            }
                        }
                    }
                }
            }
        }

        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    // 3. Curved Celestial RA/Dec Grid (Precomputed 3D vectors, 0 trig calls)
    if (showGrid.value) {
        const grid = getGridGeometry(fov);
        ctx.lineWidth = 1;

        // Meridians + non-equator parallels batched into ONE stroke call
        ctx.strokeStyle = 'rgba(92, 128, 184, 0.14)';
        ctx.beginPath();
        const maxVisDeg = maxVisRad * RAD2DEG;
        const nonPolarView = Math.abs(centerDec) < 68;
        const cosViewDec = Math.cos(centerDec * DEG2RAD);

        for (let i = 0; i < grid.meridians.length; i++) {
            const m = grid.meridians[i];
            if (nonPolarView) {
                let dRa = Math.abs(m.ra - centerRa);
                if (dRa > 180) dRa = 360 - dRa;
                if (dRa * cosViewDec > maxVisDeg + 8) continue;
            }
            traceXYZPolyline(ctx, m.xyz, m.count, cam, maxJumpSq);
        }

        let equatorParallel = null;
        for (let i = 0; i < grid.parallels.length; i++) {
            const p = grid.parallels[i];
            if (Math.abs(p.dec - centerDec) > maxVisDeg + 12) continue;
            if (p.isEquator) {
                equatorParallel = p;
            } else {
                traceXYZPolyline(ctx, p.xyz, p.count, cam, maxJumpSq);
            }
        }
        ctx.stroke();

        if (equatorParallel) {
            ctx.strokeStyle = 'rgba(130, 175, 238, 0.25)';
            ctx.beginPath();
            traceXYZPolyline(ctx, equatorParallel.xyz, equatorParallel.count, cam, maxJumpSq);
            ctx.stroke();
        }
    }

    // 4. Constellation Boundaries & Stick Lines (3D bounding-sphere culled)
    if (showConstellations.value && preparedSky) {
        if (fov >= 8) {
            ctx.save();
            ctx.strokeStyle = 'rgba(94, 126, 175, 0.11)';
            ctx.lineWidth = 0.85;
            ctx.setLineDash([4, 5]);
            ctx.beginPath();
            const bounds = preparedSky.constBounds;
            for (let i = 0; i < bounds.length; i++) {
                const ring = bounds[i];
                const sumLessPi = maxVisRad + ring.radiusRad < Math.PI;
                if (sumLessPi) {
                    const thresh = cosVis * ring.cosR - sinVis * ring.sinR;
                    if (cam.Cx * ring.cx + cam.Cy * ring.cy + cam.Cz * ring.cz < thresh) continue;
                }
                traceXYZPolyline(ctx, ring.xyz, ring.count, cam, maxJumpSq);
            }
            ctx.stroke();
            ctx.restore();
        }

        ctx.strokeStyle = 'rgba(116, 168, 242, 0.46)';
        ctx.lineWidth = 1.15;
        ctx.beginPath();
        const lines = preparedSky.constLines;
        for (let i = 0; i < lines.length; i++) {
            const seg = lines[i];
            if (maxVisRad + seg.radiusRad < Math.PI) {
                const thresh = cosVis * seg.cosR - sinVis * seg.sinR;
                if (cam.Cx * seg.cx + cam.Cy * seg.cy + cam.Cz * seg.cz < thresh) continue;
            }
            traceXYZPolyline(ctx, seg.xyz, seg.count, cam, maxJumpSq);
        }
        ctx.stroke();
    }

    // 5. Fallback Individual Sky Photos (only when pre-composited spherical sector mode is inactive)
    const hovered = hoveredItem.value;
    const photoCount = skyPhotosList.length;
    if (showCachedPhotos.value && !skyAtlasSectorMode.value && photoCount > 0) {
        const denseCatalogMode = photoCount > 120;
        const minRenderPx = denseCatalogMode ? 3.2 : 2.5;
        const minHitPx = denseCatalogMode ? 4.5 : 3.0;
        const photoCullCos = Math.cos(Math.min(Math.PI * 0.72, maxVisRad + 0.15));
        const activeName = props.object?.name || '';
        const activeIdx = activeName ? (photoIndexByName.get(activeName) ?? -1) : -1;
        let hoveredHighlight = null;

        // 8x8 px screen-cell overlap cap for tiny zoomed-out cluster tiles (Virgo/Coma/Sagittarius)
        let gridCols = 1;
        let gridRows = 1;
        if (denseCatalogMode) {
            gridCols = Math.max(1, (w >> 3) + 2);
            gridRows = Math.max(1, (h >> 3) + 2);
            const neededCells = gridCols * gridRows;
            if (denseCellCounts.length < neededCells) {
                denseCellCounts = new Uint8Array(neededCells * 2);
            }
            denseCellCounts.fill(0, 0, neededCells);
        }

        ctx.fillStyle = denseCatalogMode ? 'rgba(4, 7, 14, 0.44)' : 'rgba(4, 7, 14, 0.62)';

        const renderPhotoAt = (idx, isActiveTarget) => {
            if (!isActiveTarget && !visPatch[photoPatchIds[idx]]) return;

            const o = idx * 4;
            const x = photoSoA[o];
            const y = photoSoA[o + 1];
            const z = photoSoA[o + 2];
            const cosC = x * cam.Cx + y * cam.Cy + z * cam.Cz;
            if (cosC < photoCullCos) return;

            const k = 2.0 / (1.0 + cosC);
            const fk = cam.f * k;
            const sizePx = photoSoA[o + 3] * fk;
            if (sizePx < minRenderPx && !isActiveTarget) return;

            const u = x * cam.Ex + y * cam.Ey + z * cam.Ez;
            const v = x * cam.Nx + y * cam.Ny + z * cam.Nz;
            const px = cam.halfW - fk * u;
            const py = cam.halfH - fk * v;

            if (
                px + sizePx < -40 ||
                px - sizePx > w + 40 ||
                py + sizePx < -40 ||
                py - sizePx > h + 40
            ) {
                return;
            }

            const photo = skyPhotosList[idx];

            if (
                !isActiveTarget &&
                buildHitItems &&
                sizePx >= minHitPx &&
                (!applyTargetFilters.value || filteredNameSet.value.has(photo.name))
            ) {
                pushInteractiveItem(
                    'photo',
                    photo.name,
                    photo.commonName,
                    photo.fov,
                    photo.ra,
                    photo.dec,
                    px,
                    py,
                    Math.max(sizePx * 0.5, 9),
                    photo.refObj,
                    photo.isCustom
                );
            }

            const isHovered =
                hovered?.kind === 'photo' &&
                hovered?.name === photo.name &&
                Math.abs(hovered.ra - photo.ra) < 0.01;

            let cellIdx = -1;
            if (denseCatalogMode && sizePx < 18.0 && !isActiveTarget && !isHovered) {
                const gx = Math.min(gridCols - 1, Math.max(0, (px >> 3) + 1));
                const gy = Math.min(gridRows - 1, Math.max(0, (py >> 3) + 1));
                cellIdx = gy * gridCols + gx;
                if (denseCellCounts[cellIdx] >= 2) {
                    return;
                }
            }

            const tile = photo.atlasTile;
            const sheetImg = tile && sizePx >= 3.2 ? ensureAtlasSheetImage(tile.s) : null;
            const wantHiRes =
                Boolean(photo.url) &&
                (!tile || sizePx >= 190 || (isActiveTarget && sizePx >= 115));
            const hiResImg =
                wantHiRes && sizePx >= 4.5
                    ? ensureImageLoaded(photo.url, !isPanning.value || isActiveTarget)
                    : null;

            const hasReadyImg = Boolean((hiResImg || sheetImg) && sizePx >= 4.0);
            if (!hasReadyImg && !isHovered) {
                return;
            }

            if (cellIdx >= 0 && hasReadyImg) {
                denseCellCounts[cellIdx]++;
            }

            // Algebraic conformal local-north screen basis (zero sin/cos/atan2 calls)
            const vx = -x * z;
            const vy = -y * z;
            const vz = 1.0 - z * z;
            let cosA = 1.0;
            let sinA = 0.0;
            if (vz > 1e-8) {
                const vDotC = vx * cam.Cx + vy * cam.Cy + vz * cam.Cz;
                const vDotE = vx * cam.Ex + vy * cam.Ey + vz * cam.Ez;
                const vDotN = vx * cam.Nx + vy * cam.Ny + vz * cam.Nz;
                const halfKVC = 0.5 * k * vDotC;
                const dxN = halfKVC * u - vDotE;
                const dyN = halfKVC * v - vDotN;
                const len = Math.hypot(dxN, dyN);
                if (len > 1e-9) {
                    const invLen = 1.0 / len;
                    cosA = -dyN * invLen;
                    sinA = dxN * invLen;
                }
            }

            const m11 = dpr * cosA;
            const m12 = dpr * sinA;
            const tx = px * dpr;
            const ty = py * dpr;

            if (isHovered) {
                hoveredHighlight = {
                    m11,
                    m12,
                    tx,
                    ty,
                    boxSz: Math.max(sizePx, 8)
                };
            }

            if (hasReadyImg) {
                const half = sizePx * 0.5;
                ctx.setTransform(m11, m12, -m12, m11, tx, ty);

                // Per-tile dark backing rect attenuates overlapping tiles underneath
                ctx.globalCompositeOperation = 'source-over';
                ctx.globalAlpha = 1.0;
                ctx.fillRect(-half, -half, sizePx, sizePx);

                // Screen-blend this tile onto the darkened backing
                ctx.globalCompositeOperation = 'screen';
                ctx.globalAlpha = isActiveTarget ? 0.96 : 0.88;
                if (hiResImg) {
                    ctx.drawImage(hiResImg, -half, -half, sizePx, sizePx);
                } else if (sheetImg && tile) {
                    ctx.drawImage(
                        sheetImg,
                        tile.u + 0.5,
                        tile.v + 0.5,
                        tile.w - 1,
                        tile.h - 1,
                        -half,
                        -half,
                        sizePx,
                        sizePx
                    );
                }
            }
        };

        for (let i = 0; i < photoCount; i++) {
            if (i === activeIdx) continue;
            renderPhotoAt(i, false);
        }
        if (activeIdx >= 0 && activeIdx < photoCount) {
            renderPhotoAt(activeIdx, true);
        }

        ctx.globalCompositeOperation = 'source-over';
        ctx.globalAlpha = 1.0;

        // Optional single hover highlight (only for the target directly under cursor)
        if (hoveredHighlight) {
            const hh = hoveredHighlight;
            const half = hh.boxSz * 0.5;
            ctx.setTransform(hh.m11, hh.m12, -hh.m12, hh.m11, hh.tx, hh.ty);
            ctx.strokeStyle = 'rgba(251, 191, 36, 0.88)';
            ctx.lineWidth = 1.5;
            ctx.strokeRect(-half, -half, hh.boxSz, hh.boxSz);
        }

        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    // Reserve label space for Active Target Pill Badge first
    let activeTargetScreen = null;
    let activeBadgeText = '';
    if (props.object) {
        const tRa = parseFloat(props.object.ra);
        const tDec = parseFloat(props.object.dec);
        if (Number.isFinite(tRa) && Number.isFinite(tDec)) {
            const tp = projectRaDecWithBasis(tRa, tDec, cam);
            if (tp && tp.x >= -100 && tp.x <= w + 100 && tp.y >= -100 && tp.y <= h + 100) {
                activeTargetScreen = { ...tp, ra: tRa, dec: tDec };
                const common =
                    props.object.common_name && props.object.common_name !== 'N/A'
                        ? ` · ${props.object.common_name}`
                        : '';
                activeBadgeText = `${props.object.name}${common}`;
                ctx.font = '600 12.5px system-ui, -apple-system, sans-serif';
                const tw = getMeasuredTextWidth(ctx, 'badge', activeBadgeText) + 22;
                const baseImgPx = Math.max(
                    (props.object.image_fov || props.baseImageFov || 1.0) * DEG2RAD * tp.f * tp.k,
                    26
                );
                const ringR = Math.max(baseImgPx * 0.56, 16);
                const by = tp.y - ringR - 20;
                pushPlacedBox(
                    tp.x - tw * 0.5 - 6,
                    by - 14,
                    tp.x + tw * 0.5 + 6,
                    tp.y + ringR + 6
                );
            }
        }
    }

    // 6. Stars (Batched by spectral color/alpha + pre-rendered glow sprites)
    if (preparedSky) {
        const magLimit = fov > 55 ? 5.35 : fov > 28 ? 5.75 : 6.05;
        const nameMagLimit = fov > 60 ? 2.1 : fov > 32 ? 2.75 : fov > 14 ? 3.5 : 4.4;
        const { starCount, starXYZ, starMag, starRadius, starBucket, brightStars, labeledStars } =
            preparedSky;

        for (let b = 0; b < starBuckets.length; b++) {
            starBuckets[b].length = 0;
        }

        // 6a. Stamp pre-rendered glow halos for bright stars (< 2.3 mag)
        if (starGlowSprites) {
            for (let i = 0; i < brightStars.length; i++) {
                const bs = brightStars[i];
                const o = bs.idx * 3;
                const x = starXYZ[o];
                const y = starXYZ[o + 1];
                const z = starXYZ[o + 2];
                const cosC = x * cam.Cx + y * cam.Cy + z * cam.Cz;
                if (cosC < minPointCos) continue;

                const fk = (cam.f * 2.0) / (1.0 + cosC);
                const sx = cam.halfW - fk * (x * cam.Ex + y * cam.Ey + z * cam.Ez);
                const sy = cam.halfH - fk * (x * cam.Nx + y * cam.Ny + z * cam.Nz);
                if (sx < -24 || sx > w + 24 || sy < -24 || sy > h + 24) continue;

                const haloR = starRadius[bs.idx] * bs.haloMult;
                ctx.drawImage(
                    starGlowSprites[bs.colorIdx],
                    sx - haloR,
                    sy - haloR,
                    haloR * 2.0,
                    haloR * 2.0
                );
            }
        }

        // 6b. Project visible stars & push into batched color/alpha buckets
        for (let i = 0; i < starCount; i++) {
            if (starMag[i] > magLimit) break;
            const o = i * 3;
            const x = starXYZ[o];
            const y = starXYZ[o + 1];
            const z = starXYZ[o + 2];
            const cosC = x * cam.Cx + y * cam.Cy + z * cam.Cz;
            if (cosC < minPointCos) continue;

            const fk = (cam.f * 2.0) / (1.0 + cosC);
            const sx = cam.halfW - fk * (x * cam.Ex + y * cam.Ey + z * cam.Ez);
            if (sx < -8 || sx > w + 8) continue;
            const sy = cam.halfH - fk * (x * cam.Nx + y * cam.Ny + z * cam.Nz);
            if (sy < -8 || sy > h + 8) continue;

            const arr = starBuckets[starBucket[i]];
            arr.push(sx, sy, starRadius[i]);
        }

        // Flush all star discs in at most 15 Canvas fill() calls
        for (let b = 0; b < starBuckets.length; b++) {
            const arr = starBuckets[b];
            if (arr.length === 0) continue;
            ctx.fillStyle = STAR_BUCKET_STYLES[b];
            ctx.beginPath();
            for (let j = 0; j < arr.length; j += 3) {
                const sx = arr[j];
                const sy = arr[j + 1];
                const r = arr[j + 2];
                ctx.moveTo(sx + r, sy);
                ctx.arc(sx, sy, r, 0, TAU);
            }
            ctx.fill();
        }

        // 6c. Star proper names (warm amber-gold with dark outline halo for gray survey tiles)
        ctx.font = '600 11.5px system-ui, -apple-system, sans-serif';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';
        ctx.lineJoin = 'round';
        ctx.miterLimit = 2;
        ctx.lineWidth = 3.2;
        ctx.strokeStyle = 'rgba(3, 6, 12, 0.94)';

        for (let i = 0; i < labeledStars.length; i++) {
            const ls = labeledStars[i];
            if (ls.mag > nameMagLimit) break;
            const starName = ls.properName || (fov < 12 && ls.mag < 3.8 ? ls.desig : '');
            if (!starName) continue;

            const o = ls.idx * 3;
            const x = starXYZ[o];
            const y = starXYZ[o + 1];
            const z = starXYZ[o + 2];
            const cosC = x * cam.Cx + y * cam.Cy + z * cam.Cz;
            if (cosC < minPointCos) continue;

            const fk = (cam.f * 2.0) / (1.0 + cosC);
            const sx = cam.halfW - fk * (x * cam.Ex + y * cam.Ey + z * cam.Ez);
            const sy = cam.halfH - fk * (x * cam.Nx + y * cam.Ny + z * cam.Nz);
            if (sx <= 30 || sx >= w - 65 || sy <= 24 || sy >= h - 30) continue;

            const tw = getMeasuredTextWidth(ctx, 'star', starName);
            const lx = sx + starRadius[ls.idx] + 5;
            const x1 = lx - 2;
            const y1 = sy - 7;
            const x2 = lx + tw + 2;
            const y2 = sy + 7;

            if (!overlapsPlaced(x1, y1, x2, y2, 2)) {
                pushPlacedBox(x1, y1, x2, y2);
                ctx.strokeText(starName, lx, sy);
                ctx.fillStyle = ls.isBright ? '#fbbf24' : 'rgba(234, 179, 8, 0.92)';
                ctx.fillText(starName, lx, sy);
            }
        }
    }

    // 7. Progressive Zoom-Revealed Deep-Sky Objects & Clickable Target Names (FOV-Independent)
    if (showDsos.value && preparedDsos.length > 0) {
        const activeName = props.object?.name || '';
        const filterOnly = applyTargetFilters.value;
        const allowedNames = filterOnly ? filteredNameSet.value : null;
        const maxMarkers = filterOnly
            ? fov > 55
                ? 48
                : fov > 32
                  ? 72
                  : fov > 18
                    ? 110
                    : fov > 9
                      ? 165
                      : 240
            : fov > 55
              ? 30
              : fov > 32
                ? 48
                : fov > 18
                  ? 80
                  : fov > 9
                    ? 135
                    : 220;
        const showZoomCommon = fov <= 12;
        visibleDsosCount = 0;
        const dsoTotal = preparedDsos.length;

        for (let i = 0; i < dsoTotal; i++) {
            const d = preparedDsos[i];
            if (filterOnly && !allowedNames.has(d.name)) continue;
            if (!visPatch[dsoPatchIds[i]]) continue;

            const o = i * 4;
            const dx = dsoSoA[o];
            const dy = dsoSoA[o + 1];
            const dz = dsoSoA[o + 2];
            const cosC = dx * cam.Cx + dy * cam.Cy + dz * cam.Cz;
            if (cosC < minPointCos) continue;
            if (d.name === activeName) continue;

            // Progressive astronomical reveal by zoom depth (bypassed when Apply Filters is active
            // so even faint/unnamed filtered targets remain visible across wide-field views)
            if (!filterOnly) {
                if (fov > 55) {
                    if (!d.isMessier && !d.hasCommon && d.mag > 7.5 && d.maj < 28) continue;
                } else if (fov > 32) {
                    if (!d.isMessier && !d.hasCommon && d.mag > 8.8 && d.maj < 16) continue;
                } else if (fov > 18) {
                    if (!d.isMessier && !d.hasCommon && d.mag > 10.6 && d.maj < 8) continue;
                } else if (fov > 9) {
                    if (!d.isMessier && !d.hasCommon && d.mag > 12.6 && d.maj < 3.5) continue;
                }
            }

            const fk = (cam.f * 2.0) / (1.0 + cosC);
            const sx = cam.halfW - fk * (dx * cam.Ex + dy * cam.Ey + dz * cam.Ez);
            if (sx < 10 || sx > w - 10) continue;
            const sy = cam.halfH - fk * (dx * cam.Nx + dy * cam.Ny + dz * cam.Nz);
            if (sy < 14 || sy > h - 18) continue;

            const rPx = Math.max(3.6, Math.min(22, dsoSoA[o + 3] * fk));
            pushVisibleDso(d, sx, sy, rPx);
            if (visibleDsosCount >= maxMarkers) break;
        }

        if (visibleDsosCount > 0) {
            const hoveredDsoName =
                hovered && hovered.kind === 'dso' && !isPanning.value ? hovered.name : '';

            // Batch deep catalog DSO rings (NGC / IC / Sh2 / Barnard / LDN / PGC)
            ctx.lineWidth = 1.25;
            ctx.strokeStyle = 'rgba(192, 132, 252, 0.85)';
            ctx.beginPath();
            for (let i = 0; i < visibleDsosCount; i++) {
                const v = visibleDsosPool[i];
                if (v.d.isMessier || v.d.hasCommon || v.d.name === hoveredDsoName) {
                    continue;
                }
                ctx.moveTo(v.sx + v.rPx, v.sy);
                ctx.arc(v.sx, v.sy, v.rPx, 0, TAU);
            }
            ctx.stroke();

            // Batch Messier & named showcase DSO rings
            ctx.strokeStyle = 'rgba(56, 189, 248, 0.9)';
            ctx.beginPath();
            for (let i = 0; i < visibleDsosCount; i++) {
                const v = visibleDsosPool[i];
                if ((!v.d.isMessier && !v.d.hasCommon) || v.d.name === hoveredDsoName) {
                    continue;
                }
                ctx.moveTo(v.sx + v.rPx, v.sy);
                ctx.arc(v.sx, v.sy, v.rPx, 0, TAU);
            }
            ctx.stroke();

            // Clickable target labels + zero-DOM inline canvas hover highlight
            ctx.font = '600 11px system-ui, -apple-system, sans-serif';
            ctx.textAlign = 'left';
            ctx.textBaseline = 'middle';
            ctx.lineJoin = 'round';
            ctx.miterLimit = 2;

            let hoveredSlot = null;

            for (let i = 0; i < visibleDsosCount; i++) {
                const slot = visibleDsosPool[i];
                const { d, sx, sy, rPx } = slot;
                if (d.name === hoveredDsoName) {
                    hoveredSlot = slot;
                    continue;
                }

                const labelText = showZoomCommon ? d.zoomLabel : d.shortName;
                const lx = sx + rPx + 4.5;
                const tw = getMeasuredTextWidth(ctx, 'dso', labelText);
                const x1 = lx - 2;
                const y1 = sy - 7;
                const x2 = lx + tw + 2;
                const y2 = sy + 7;

                let bx1 = 0;
                let by1 = 0;
                let bx2 = 0;
                let by2 = 0;

                if (!overlapsPlaced(x1, y1, x2, y2, 2)) {
                    pushPlacedBox(x1, y1, x2, y2);
                    ctx.lineWidth = 3.2;
                    ctx.strokeStyle = 'rgba(3, 6, 12, 0.94)';
                    ctx.strokeText(labelText, lx, sy);
                    ctx.fillStyle = d.isMessier || d.hasCommon ? '#38bdf8' : '#c084fc';
                    ctx.fillText(labelText, lx, sy);
                    bx1 = lx - 4;
                    by1 = sy - 9.5;
                    bx2 = lx + tw + 5;
                    by2 = sy + 9.5;
                }

                if (buildHitItems) {
                    pushInteractiveItem(
                        'dso',
                        d.name,
                        d.commonName,
                        null,
                        d.ra,
                        d.dec,
                        sx,
                        sy,
                        Math.max(rPx + 5, 10),
                        d.obj,
                        false,
                        bx1,
                        by1,
                        bx2,
                        by2
                    );
                }
            }

            // Draw hovered DSO last on top with crisp amber-gold ring & inline pill label
            if (hoveredSlot) {
                const { d, sx, sy, rPx } = hoveredSlot;
                ctx.strokeStyle = 'rgba(251, 191, 36, 0.28)';
                ctx.lineWidth = 4.2;
                ctx.beginPath();
                ctx.arc(sx, sy, rPx, 0, TAU);
                ctx.stroke();

                ctx.strokeStyle = '#fbbf24';
                ctx.lineWidth = 1.9;
                ctx.beginPath();
                ctx.arc(sx, sy, rPx, 0, TAU);
                ctx.stroke();

                const hLabel = d.zoomLabel || d.shortName;
                ctx.font = '600 11.5px system-ui, -apple-system, sans-serif';
                const tw = getMeasuredTextWidth(ctx, 'dsoH', hLabel);
                const pillW = tw + 12;
                const pillH = 20;
                let px = sx + rPx + 5;
                if (px + pillW > w - 8) {
                    px = Math.max(8, sx - rPx - pillW - 5);
                }
                const py = Math.max(8, Math.min(h - pillH - 8, sy - pillH * 0.5));

                ctx.fillStyle = 'rgba(10, 14, 24, 0.94)';
                ctx.strokeStyle = 'rgba(251, 191, 36, 0.72)';
                ctx.lineWidth = 1.15;
                ctx.beginPath();
                if (ctx.roundRect) {
                    ctx.roundRect(px, py, pillW, pillH, 5);
                } else {
                    ctx.rect(px, py, pillW, pillH);
                }
                ctx.fill();
                ctx.stroke();

                ctx.fillStyle = '#fde68a';
                ctx.fillText(hLabel, px + 6, py + pillH * 0.5 + 0.5);

                if (buildHitItems) {
                    pushInteractiveItem(
                        'dso',
                        d.name,
                        d.commonName,
                        null,
                        d.ra,
                        d.dec,
                        sx,
                        sy,
                        Math.max(rPx + 6, 11),
                        d.obj,
                        false,
                        px - 2,
                        py - 2,
                        px + pillW + 2,
                        py + pillH + 2
                    );
                }
            }
        }
    }

    // 8. Constellation Names (ORION, MONOCEROS, LEPUS — saturated cobalt with dark halo)
    if (showConstellations.value && fov >= 7 && preparedSky) {
        ctx.font = '600 11px system-ui, -apple-system, sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.lineJoin = 'round';
        ctx.miterLimit = 2;
        ctx.lineWidth = 3.4;
        ctx.strokeStyle = 'rgba(3, 6, 12, 0.92)';
        ctx.fillStyle = 'rgba(96, 165, 250, 0.92)';

        const labels = preparedSky.constLabels;
        for (let i = 0; i < labels.length; i++) {
            const c = labels[i];
            if (fov < 14 && c.rank > 2) continue;
            const cosC = c.x * cam.Cx + c.y * cam.Cy + c.z * cam.Cz;
            if (cosC < minPointCos) continue;

            const fk = (cam.f * 2.0) / (1.0 + cosC);
            const sx = cam.halfW - fk * (c.x * cam.Ex + c.y * cam.Ey + c.z * cam.Ez);
            const sy = cam.halfH - fk * (c.x * cam.Nx + c.y * cam.Ny + c.z * cam.Nz);
            if (sx < 55 || sx > w - 55 || sy < 36 || sy > h - 36) continue;

            const tw = getMeasuredTextWidth(ctx, 'const', c.tracked);
            const x1 = sx - tw * 0.5 - 4;
            const y1 = sy - 8;
            const x2 = sx + tw * 0.5 + 4;
            const y2 = sy + 8;

            if (!overlapsPlaced(x1, y1, x2, y2, 3)) {
                pushPlacedBox(x1, y1, x2, y2);
                ctx.strokeText(c.tracked, sx, sy);
                ctx.fillText(c.tracked, sx, sy);
            }
        }
    }

    // 8.5 Local Observer Horizon, Ground Veil, Min-Altitude Ring & Compass Points
    if ((showHorizon.value || isHorizonFixed.value) && observerLocation.value) {
        drawHorizonOverlay(ctx, cam, w, h, fov, minPointCos, overlapsPlaced, horizBasis);
    }

    // 9. Active Target Highlight Ring, Rotated Sensor FOV Frame & Comparison Setups
    lastFovHit.valid = false;
    let placedFovCtrl = false;
    if (props.object) {
        if (!fovPinned.value && hasValidSensorFov.value) {
            syncUnpinnedFovToCenter(cam);
            emitSensorCenterIfChanged();
        }
        const sensorRa = fovRa.value;
        const sensorDec = fovDec.value;
        let sp = projectRaDecWithBasis(sensorRa, sensorDec, cam);
        if (sp && !fovPinned.value) {
            sp.x = cam.halfW;
            sp.y = cam.halfH;
        }
        let totalRotRad = 0;
        if (sp) {
            const localNorth = computeLocalNorthAngleWithBasis(
                sensorRa,
                sensorDec,
                sp,
                cam
            );
            totalRotRad = localNorth + (Number(props.rotation) || 0) * DEG2RAD;
        }

        if (sp && Array.isArray(props.comparisonRectangles) && props.comparisonRectangles.length > 0) {
            for (let i = 0; i < props.comparisonRectangles.length; i++) {
                const comp = props.comparisonRectangles[i];
                if (!comp?.sensorFov) continue;
                const cw = comp.sensorFov.w * DEG2RAD * sp.f * sp.k;
                const ch = comp.sensorFov.h * DEG2RAD * sp.f * sp.k;
                if (cw < 1.5 && ch < 1.5) continue;

                ctx.save();
                ctx.translate(sp.x, sp.y);
                ctx.rotate(totalRotRad);
                ctx.strokeStyle = comp.color?.border || '#06b6d4';
                ctx.lineWidth = 1.5;
                ctx.setLineDash([5, 4]);
                ctx.strokeRect(-cw * 0.5, -ch * 0.5, cw, ch);
                ctx.restore();
            }
        }

        if (sp && props.sensorFov?.w > 0 && props.sensorFov?.h > 0) {
            const sw = props.sensorFov.w * DEG2RAD * sp.f * sp.k;
            const sh = props.sensorFov.h * DEG2RAD * sp.f * sp.k;
            const drawW = Math.max(sw, 6);
            const drawH = Math.max(sh, 4.5);
            const cosR = Math.cos(totalRotRad);
            const sinR = Math.sin(totalRotRad);

            const isPinned = fovPinned.value;
            if (isPinned) {
                lastFovHit.valid = true;
                lastFovHit.cx = sp.x;
                lastFovHit.cy = sp.y;
                lastFovHit.halfW = Math.max(drawW * 0.5 + 6, 14);
                lastFovHit.halfH = Math.max(drawH * 0.5 + 6, 14);
                lastFovHit.cosR = cosR;
                lastFovHit.sinR = sinR;
            }

            const isDragActive = isPinned && isDraggingFov.value;
            const isDragHover = isPinned && isHoveringFov.value;

            ctx.save();
            ctx.translate(sp.x, sp.y);
            ctx.rotate(totalRotRad);

            if (isDragActive || isDragHover) {
                ctx.fillStyle = isDragActive
                    ? 'rgba(251, 191, 36, 0.14)'
                    : 'rgba(245, 158, 11, 0.08)';
                ctx.fillRect(-drawW * 0.5, -drawH * 0.5, drawW, drawH);
            } else if (isPinned) {
                ctx.fillStyle = 'rgba(45, 212, 191, 0.05)';
                ctx.fillRect(-drawW * 0.5, -drawH * 0.5, drawW, drawH);
            }

            // Zero-cost double-stroke glow (replaces expensive Gaussian ctx.shadowBlur)
            ctx.strokeStyle = isDragActive
                ? 'rgba(251, 191, 36, 0.45)'
                : isPinned
                  ? 'rgba(45, 212, 191, 0.38)'
                  : 'rgba(245, 158, 11, 0.28)';
            ctx.lineWidth = isDragActive || isDragHover ? 5.2 : 4.6;
            ctx.strokeRect(-drawW * 0.5, -drawH * 0.5, drawW, drawH);

            ctx.strokeStyle = isDragActive || isDragHover ? '#fbbf24' : '#f59e0b';
            ctx.lineWidth = isDragActive || isDragHover ? 2.1 : 1.85;
            ctx.strokeRect(-drawW * 0.5, -drawH * 0.5, drawW, drawH);

            // Pinned FOV visual indicator: crisp inner teal dashed accent
            if (isPinned && drawW >= 18 && drawH >= 14) {
                ctx.save();
                ctx.strokeStyle = 'rgba(45, 212, 191, 0.88)';
                ctx.lineWidth = 1.15;
                ctx.setLineDash([4, 4]);
                ctx.strokeRect(-drawW * 0.5 + 2.5, -drawH * 0.5 + 2.5, drawW - 5, drawH - 5);
                ctx.restore();
            }

            // Corner L-brackets (when pinned/draggable) + subtle center crosshair (when unpinned/centered or detached)
            const handleColor =
                isDragActive || isDragHover ? '#fef08a' : 'rgba(253, 230, 138, 0.82)';
            ctx.strokeStyle = handleColor;
            ctx.lineWidth = isDragActive || isDragHover ? 1.85 : 1.45;
            ctx.beginPath();

            if (isPinned && drawW >= 18 && drawH >= 14) {
                const hw = drawW * 0.5;
                const hh = drawH * 0.5;
                const tick = Math.min(7.5, Math.max(4.0, Math.min(drawW, drawH) * 0.22));
                // Top-left
                ctx.moveTo(-hw, -hh + tick);
                ctx.lineTo(-hw, -hh);
                ctx.lineTo(-hw + tick, -hh);
                // Top-right
                ctx.moveTo(hw - tick, -hh);
                ctx.lineTo(hw, -hh);
                ctx.lineTo(hw, -hh + tick);
                // Bottom-right
                ctx.moveTo(hw, hh - tick);
                ctx.lineTo(hw, hh);
                ctx.lineTo(hw - tick, hh);
                // Bottom-left
                ctx.moveTo(-hw + tick, hh);
                ctx.lineTo(-hw, hh);
                ctx.lineTo(-hw, hh - tick);
            }

            if (!isPinned || isDragActive || isDragHover || isFovDetached.value) {
                const arm = Math.min(5.5, Math.max(3.5, Math.min(drawW, drawH) * 0.2));
                ctx.moveTo(-arm, 0);
                ctx.lineTo(arm, 0);
                ctx.moveTo(0, -arm);
                ctx.lineTo(0, arm);
            }
            ctx.stroke();

            ctx.restore();

            // Position the floating Pin / Reset control directly beside the rotated FOV rectangle
            const aabbHalfW = 0.5 * (drawW * Math.abs(cosR) + drawH * Math.abs(sinR));
            const aabbHalfH = 0.5 * (drawW * Math.abs(sinR) + drawH * Math.abs(cosR));
            if (
                sp.x + aabbHalfW >= 8 &&
                sp.x - aabbHalfW <= w - 8 &&
                sp.y + aabbHalfH >= 8 &&
                sp.y - aabbHalfH <= h - 8
            ) {
                const baseImgPx =
                    (props.object.image_fov || props.baseImageFov || 1.0) * DEG2RAD * sp.f * sp.k;
                const ringClearance = isFovDetached.value ? 0 : Math.max(baseImgPx * 0.58, 15);
                const clearanceX = Math.max(aabbHalfW, ringClearance) + 7;
                const ctrlW = isFovDetached.value ? 56 : 26;
                const ctrlH = 26;
                let leftPx = sp.x + clearanceX;
                if (leftPx + ctrlW > w - 12) {
                    leftPx = sp.x - clearanceX - ctrlW;
                }
                const topPx = sp.y - Math.max(ctrlH * 0.5, aabbHalfH);
                const clampedLeft = Math.max(10, Math.min(Math.max(10, w - ctrlW - 10), leftPx));
                const clampedTop = Math.max(50, Math.min(Math.max(50, h - ctrlH - 46), topPx));
                updateFovFloatingCtrlPosition(clampedLeft, clampedTop, true);
                placedFovCtrl = true;
            }
        }

        if (activeTargetScreen) {
            const tp = activeTargetScreen;
            const baseImgPx =
                (props.object.image_fov || props.baseImageFov || 1.0) * DEG2RAD * tp.f * tp.k;
            const ringR = Math.max(baseImgPx * 0.58, 15);

            ctx.beginPath();
            ctx.arc(tp.x, tp.y, ringR, 0, TAU);
            ctx.strokeStyle = 'rgba(245, 158, 11, 0.55)';
            ctx.lineWidth = 1.25;
            ctx.stroke();

            ctx.font = '600 12.5px system-ui, -apple-system, sans-serif';
            const textW = getMeasuredTextWidth(ctx, 'badge', activeBadgeText);
            const pillW = textW + 20;
            const pillH = 24;
            const px = Math.max(12, Math.min(w - pillW - 12, tp.x - pillW * 0.5));
            const py = tp.y - ringR - pillH - 8;

            if (py > 8) {
                ctx.fillStyle = 'rgba(14, 13, 12, 0.92)';
                ctx.strokeStyle = 'rgba(217, 155, 67, 0.68)';
                ctx.lineWidth = 1.15;
                ctx.beginPath();
                if (ctx.roundRect) {
                    ctx.roundRect(px, py, pillW, pillH, 6);
                } else {
                    ctx.rect(px, py, pillW, pillH);
                }
                ctx.fill();
                ctx.stroke();

                ctx.fillStyle = '#fbbf24';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText(activeBadgeText, px + pillW * 0.5, py + pillH * 0.5 + 0.5);
            }

            if (buildHitItems) {
                pushInteractiveItem(
                    'active',
                    props.object.name,
                    props.object.common_name && props.object.common_name !== 'N/A'
                        ? props.object.common_name
                        : '',
                    props.object.image_fov || props.baseImageFov,
                    tp.ra,
                    tp.dec,
                    tp.x,
                    tp.y,
                    ringR,
                    props.object,
                    false,
                    py > 8 ? px : 0,
                    py > 8 ? py : 0,
                    py > 8 ? px + pillW : 0,
                    py > 8 ? py + pillH : 0
                );
            }
        }
    }
    if (!placedFovCtrl && fovOverlayVisible.value) {
        updateFovFloatingCtrlPosition(0, 0, false);
    }
};

// --- INTERACTION HANDLERS (rAF-Coalesced Pan, Wheel Zoom, Touch Pinch, Click/Hover) ---
const centerOnTarget = () => {
    const tgt = getCatalogTargetCoords();
    if (animFrameId) cancelAnimationFrame(animFrameId);

    if (isHorizonFixed.value && observerLocation.value) {
        const loc = observerLocation.value;
        const horiz = makeHorizontalBasis(loc.lat, loc.lon, effectiveTimeMs.value);
        const tgtAA = computeAltAzFromRaDec(tgt.ra, tgt.dec, horiz);
        const targetAz = tgtAA.azDeg;
        const targetAlt = clampAltDeg(tgtAA.altDeg);

        viewRa.value = tgt.ra;
        viewDec.value = tgt.dec;

        const startAz = viewAz.value;
        const startAlt = viewAlt.value;
        let dAz = targetAz - startAz;
        if (dAz > 180) dAz -= 360;
        if (dAz < -180) dAz += 360;
        const dAlt = targetAlt - startAlt;

        if (Math.hypot(dAz, dAlt) < 0.05) {
            viewAz.value = targetAz;
            viewAlt.value = targetAlt;
            if (!fovPinned.value && hasValidSensorFov.value) {
                syncUnpinnedFovToCenter();
                emitSensorCenterIfChanged();
            }
            updateStatusReadout(tgt.ra, tgt.dec, true);
            scheduleDraw();
            return;
        }

        const startTime = performance.now();
        const duration = 240;
        const step = (now) => {
            const t = Math.min(1, (now - startTime) / duration);
            const ease = 1 - Math.pow(1 - t, 3);
            viewAz.value = wrapRaDeg(startAz + dAz * ease);
            viewAlt.value = clampAltDeg(startAlt + dAlt * ease);
            drawSkyCanvas();
            if (t < 1) {
                animFrameId = requestAnimationFrame(step);
            } else {
                animFrameId = null;
                if (!fovPinned.value && hasValidSensorFov.value) {
                    syncUnpinnedFovToCenter();
                    emitSensorCenterIfChanged(true);
                }
                updateStatusReadout(tgt.ra, tgt.dec, true);
            }
        };
        animFrameId = requestAnimationFrame(step);
        return;
    }

    const startRa = viewRa.value;
    const startDec = viewDec.value;
    let dRa = tgt.ra - startRa;
    if (dRa > 180) dRa -= 360;
    if (dRa < -180) dRa += 360;
    const dDec = tgt.dec - startDec;

    if (Math.hypot(dRa, dDec) < 0.05) {
        viewRa.value = tgt.ra;
        viewDec.value = tgt.dec;
        syncHorizFromRaDec(tgt.ra, tgt.dec);
        if (!fovPinned.value && hasValidSensorFov.value) {
            syncUnpinnedFovToCenter();
            emitSensorCenterIfChanged();
        }
        updateStatusReadout(tgt.ra, tgt.dec, true);
        scheduleDraw();
        return;
    }

    const startTime = performance.now();
    const duration = 240;
    const step = (now) => {
        const t = Math.min(1, (now - startTime) / duration);
        const ease = 1 - Math.pow(1 - t, 3);
        viewRa.value = wrapRaDeg(startRa + dRa * ease);
        viewDec.value = clampDecDeg(startDec + dDec * ease);
        drawSkyCanvas();
        if (t < 1) {
            animFrameId = requestAnimationFrame(step);
        } else {
            animFrameId = null;
            syncHorizFromRaDec(viewRa.value, viewDec.value);
            if (!fovPinned.value && hasValidSensorFov.value) {
                syncUnpinnedFovToCenter();
                emitSensorCenterIfChanged(true);
            }
            updateStatusReadout(viewRa.value, viewDec.value, true);
        }
    };
    animFrameId = requestAnimationFrame(step);
};

const applyZoomFactor = (factor, anchorPx = null, anchorPy = null) => {
    const prevFov = mapFovDeg.value;
    const minMapFov = Math.min(0.35, (props.baseImageFov || 1.0) * 0.8);
    const nextFov = Math.max(minMapFov, Math.min(105.0, prevFov * factor));

    if (factor < 1.0 && props.object && !isFovDetached.value) {
        const tgt = getCatalogTargetCoords();
        const ctr = getCurrentCenterRaDec();
        const distToTarget = angularDistanceDeg(ctr.ra, ctr.dec, tgt.ra, tgt.dec);
        const returnThreshold = Math.max((props.baseImageFov || 1.0) * 1.03, 0.28);
        if (
            distToTarget <= Math.max((props.baseImageFov || 1.0) * 1.15, 0.9) &&
            nextFov <= returnThreshold
        ) {
            emit('close');
            return;
        }
    }

    // Unpinned mode: FOV stays locked at viewport center, so zoom around the center
    // keeping both the FOV rectangle and the sky inside it centered.
    if (hasValidSensorFov.value && !fovPinned.value) {
        mapFovDeg.value = nextFov;
        const cam = getActiveCameraBasis();
        syncUnpinnedFovToCenter(cam);
        emitSensorCenterIfChanged();
        updateStatusReadout(fovRa.value, fovDec.value);
        scheduleDraw();
        return;
    }

    // Pinned mode: FOV stays locked to its sky coordinates (fovRa, fovDec) and moves/scales
    // naturally with the sky during cursor-anchored or center zoom.
    if (anchorPx !== null && anchorPy !== null) {
        if (isHorizonFixed.value) {
            const beforeAA = screenToAltAz(anchorPx, anchorPy, viewAz.value, viewAlt.value);
            mapFovDeg.value = nextFov;
            const afterAA = screenToAltAz(anchorPx, anchorPy, viewAz.value, viewAlt.value);
            let dAz = beforeAA.az - afterAA.az;
            if (dAz > 180) dAz -= 360;
            if (dAz < -180) dAz += 360;
            viewAz.value = wrapRaDeg(viewAz.value + dAz);
            viewAlt.value = clampAltDeg(viewAlt.value + (beforeAA.alt - afterAA.alt));
        } else {
            const beforeSky = screenToSky(anchorPx, anchorPy);
            mapFovDeg.value = nextFov;
            const afterSky = screenToSky(anchorPx, anchorPy);
            let dRa = beforeSky.ra - afterSky.ra;
            if (dRa > 180) dRa -= 360;
            if (dRa < -180) dRa += 360;
            viewRa.value = wrapRaDeg(viewRa.value + dRa);
            viewDec.value = clampDecDeg(viewDec.value + (beforeSky.dec - afterSky.dec));
        }
    } else {
        mapFovDeg.value = nextFov;
        const ctr = getCurrentCenterRaDec();
        updateStatusReadout(ctr.ra, ctr.dec);
    }

    scheduleDraw();
};

const onWheel = (e) => {
    if (!canvasRectValid) refreshCanvasRect();
    const useCursorAnchor = !hasValidSensorFov.value || fovPinned.value;
    const px = useCursorAnchor && canvasRectValid ? e.clientX - canvasLeft : null;
    const py = useCursorAnchor && canvasRectValid ? e.clientY - canvasTop : null;
    const factor = e.deltaY > 0 ? 1.14 : 1 / 1.14;
    applyZoomFactor(factor, px, py);
};

const findHitItem = (px, py) => {
    let best = null;
    let bestScoreSq = Infinity;
    for (let i = 0; i < interactiveCount; i++) {
        const item = interactivePool[i];
        const dx = px - item.x;
        const dy = py - item.y;
        const dSq = dx * dx + dy * dy;
        if (dSq <= item.hitR * item.hitR) {
            if (dSq < bestScoreSq) {
                best = item;
                bestScoreSq = dSq;
            }
            continue;
        }
        if (
            item.bx2 > item.bx1 &&
            px >= item.bx1 &&
            px <= item.bx2 &&
            py >= item.by1 &&
            py <= item.by2
        ) {
            const cy = 0.5 * (item.by1 + item.by2);
            const labelDistSq = (py - cy) * (py - cy) + 16.0;
            if (labelDistSq < bestScoreSq) {
                best = item;
                bestScoreSq = labelDistSq;
            }
        }
    }
    return best;
};

const beginPanGesture = (clientX, clientY, forceTimeDrag = false) => {
    if (animFrameId) {
        cancelAnimationFrame(animFrameId);
        animFrameId = null;
    }
    refreshCanvasRect();
    isPanning.value = true;
    panMoved = false;
    panStartScreen = { x: clientX, y: clientY };

    if (!forceTimeDrag && hasValidSensorFov.value && canvasRectValid) {
        const px = clientX - canvasLeft;
        const py = clientY - canvasTop;
        if (hitTestSensorFov(px, py)) {
            panDragKind = 'fov';
            isDraggingFov.value = true;
            fovDragOffset.dx = lastFovHit.cx - px;
            fovDragOffset.dy = lastFovHit.cy - py;
            return;
        }
    }
    isDraggingFov.value = false;

    if (isHorizonFixed.value && observerLocation.value) {
        if (forceTimeDrag || dragTimeMode.value) {
            panDragKind = 'time';
            panStartTimeMs = effectiveTimeMs.value;
            const cam = getActiveCameraBasis();
            const px = canvasRectValid ? clientX - canvasLeft : viewportW.value * 0.5;
            const py = canvasRectValid ? clientY - canvasTop : viewportH.value * 0.5;
            const p0 = screenToUnitVec(px, py, cam);
            const k = 2.0 / Math.max(0.25, 1.0 + p0.cosC);
            const fk = cam.f * k;
            const tx = p0.y;
            const ty = -p0.x;
            const tDotC = tx * cam.Cx + ty * cam.Cy;
            const tDotE = tx * cam.Ex + ty * cam.Ey;
            const tDotN = tx * cam.Nx + ty * cam.Ny;
            const pDotE = p0.x * cam.Ex + p0.y * cam.Ey + p0.z * cam.Ez;
            const pDotN = p0.x * cam.Nx + p0.y * cam.Ny + p0.z * cam.Nz;
            const vx = -fk * (tDotE - 0.5 * k * tDotC * pDotE);
            const vy = -fk * (tDotN - 0.5 * k * tDotC * pDotN);
            const vNormSq = vx * vx + vy * vy;
            const minDenom = 0.18 * cam.f * (0.18 * cam.f);
            panTimeVel = {
                vx,
                vy,
                denom: Math.max(vNormSq, minDenom),
                nearPole: vNormSq < 0.06 * cam.f * (0.06 * cam.f),
                f: cam.f
            };
        } else {
            panDragKind = 'altaz';
            panStartHoriz = { az: viewAz.value, alt: viewAlt.value };
        }
    } else {
        panDragKind = 'eq';
        panStartSky = { ra: viewRa.value, dec: viewDec.value };
    }
};

const flushPointerFrame = () => {
    pointerRafId = null;
    const ptr = pendingPointer;
    if (!ptr) return;

    if (isPanning.value) {
        const dx = ptr.clientX - panStartScreen.x;
        const dy = ptr.clientY - panStartScreen.y;
        if (dx * dx + dy * dy > 9) panMoved = true;

        if (panDragKind === 'fov') {
            const tx = Math.max(12, Math.min(viewportW.value - 12, ptr.px + fovDragOffset.dx));
            const ty = Math.max(12, Math.min(viewportH.value - 12, ptr.py + fovDragOffset.dy));
            const cam = getActiveCameraBasis();
            const skyPt = screenToSky(tx, ty, cam);
            fovRa.value = skyPt.ra;
            fovDec.value = skyPt.dec;
            emitSensorCenterIfChanged();
            drawSkyCanvas();
            updateStatusReadout(skyPt.ra, skyPt.dec);
            return;
        }

        if (panDragKind === 'time') {
            const dThetaRad = panTimeVel.nearPole
                ? dx / panTimeVel.f
                : (dx * panTimeVel.vx + dy * panTimeVel.vy) / panTimeVel.denom;
            const nextTimeMs = panStartTimeMs + dThetaRad * MS_PER_SIDEREAL_RAD;
            isLiveTime.value = false;
            scrubTimeMs.value = nextTimeMs;
            emit('update-obs-time', { isLive: false, timeMs: nextTimeMs });
            if (!fovPinned.value && hasValidSensorFov.value) {
                syncUnpinnedFovToCenter();
                emitSensorCenterIfChanged();
            }
            drawSkyCanvas();
            const ctr = getCurrentCenterRaDec();
            updateStatusReadout(ctr.ra, ctr.dec);
            return;
        }

        if (panDragKind === 'altaz') {
            const h = Math.max(viewportH.value, 120);
            const fovRad = Math.max(0.25, Math.min(125.0, mapFovDeg.value)) * DEG2RAD;
            const f = (h * 0.5) / Math.tan(fovRad * 0.5);
            const cosAlt0 = Math.max(0.25, Math.cos(panStartHoriz.alt * DEG2RAD));
            const dAzRad = -2.0 * Math.atan2(dx, 2.0 * f * cosAlt0);
            const dAltRad = 2.0 * Math.atan2(dy, 2.0 * f);
            viewAz.value = wrapRaDeg(panStartHoriz.az + dAzRad * RAD2DEG);
            viewAlt.value = clampAltDeg(panStartHoriz.alt + dAltRad * RAD2DEG);
            drawSkyCanvas();
            const ctr = getCurrentCenterRaDec();
            updateStatusReadout(ctr.ra, ctr.dec);
            return;
        }

        const shiftedSky = screenToEquatorialPan(
            viewportW.value * 0.5 - dx,
            viewportH.value * 0.5 - dy,
            panStartSky.ra,
            panStartSky.dec
        );
        viewRa.value = shiftedSky.ra;
        viewDec.value = shiftedSky.dec;
        drawSkyCanvas();
        updateStatusReadout(shiftedSky.ra, shiftedSky.dec);
        return;
    }

    const skyPt = screenToSky(ptr.px, ptr.py);
    updateStatusReadout(skyPt.ra, skyPt.dec);

    const fovHover = hitTestSensorFov(ptr.px, ptr.py);
    let needsDraw = false;
    if (fovHover !== isHoveringFov.value) {
        isHoveringFov.value = fovHover;
        needsDraw = true;
    }

    const hit = fovHover ? null : findHitItem(ptr.px, ptr.py);
    if (
        hit?.name !== hoveredItem.value?.name ||
        hit?.kind !== hoveredItem.value?.kind
    ) {
        hoveredItem.value = hit ? { ...hit } : null;
        needsDraw = true;
    }

    if (needsDraw) {
        drawSkyCanvas();
    }
};

const onMouseDown = (e) => {
    if (e.button !== 0) return;
    isShiftHeld.value = Boolean(e.shiftKey);
    beginPanGesture(e.clientX, e.clientY, Boolean(e.shiftKey));
};

const onMouseMove = (e) => {
    if (Boolean(e.shiftKey) !== isShiftHeld.value) {
        isShiftHeld.value = Boolean(e.shiftKey);
    }
    if (!canvasRectValid && !refreshCanvasRect()) return;
    pendingPointer = {
        px: e.clientX - canvasLeft,
        py: e.clientY - canvasTop,
        clientX: e.clientX,
        clientY: e.clientY
    };
    if (pointerRafId === null) {
        pointerRafId = requestAnimationFrame(flushPointerFrame);
    }
};

const handleItemClick = async (item, closeMapAfterSelect = false) => {
    if (!item) return;
    if (item.kind === 'active') {
        emit('close');
        return;
    }
    if (item.refObj) {
        emit('select-object', item.refObj);
        if (closeMapAfterSelect) emit('close');
        return;
    }
    if (!item.isCustom && item.name) {
        try {
            const qp = new URLSearchParams();
            const loc = props.settings?.location;
            if (loc && loc.latitude !== undefined && loc.longitude !== undefined) {
                qp.set('latitude', String(loc.latitude));
                qp.set('longitude', String(loc.longitude));
            }
            qp.set('tz_offset', String(new Date().getTimezoneOffset()));
            const minHrsAlt = props.clientSettings?.min_hours_altitude ?? props.settings?.min_altitude;
            if (minHrsAlt !== undefined && minHrsAlt !== null) {
                qp.set('min_hours_altitude', String(minHrsAlt));
            }
            const eveKey = resolveObservingEvening(
                isLiveTime.value,
                liveNowMs.value,
                effectiveTimeMs.value
            ).key;
            if (eveKey && eveKey !== resolveLiveTonightEveningKey(liveNowMs.value)) {
                qp.set('obs_date', eveKey);
            }
            const qs = qp.toString();
            const res = await fetch(`/api/object-details/${encodeURIComponent(item.name)}${qs ? `?${qs}` : ''}`);
            if (res.ok) {
                const details = await res.json();
                if (details?.name) {
                    emit('select-object', details);
                    if (closeMapAfterSelect) emit('close');
                    return;
                }
            }
        } catch (err) {
            console.warn('Could not load object details:', err);
        }
    }
    viewRa.value = wrapRaDeg(item.ra);
    viewDec.value = clampDecDeg(item.dec);
    syncHorizFromRaDec(viewRa.value, viewDec.value);
    if (!fovPinned.value && hasValidSensorFov.value) {
        syncUnpinnedFovToCenter();
        emitSensorCenterIfChanged(true);
    }
    updateStatusReadout(viewRa.value, viewDec.value, true);
    scheduleDraw();
};

const onDoubleClick = (e) => {
    if (!canvasRectValid && !refreshCanvasRect()) return;
    const hit = findHitItem(e.clientX - canvasLeft, e.clientY - canvasTop);
    if (hit) {
        handleItemClick({ ...hit }, true);
    }
};

const finishPanSync = () => {
    if (panDragKind === 'fov') {
        emitSensorCenterIfChanged(true);
        updateStatusReadout(fovRa.value, fovDec.value, true);
        return;
    }

    const ctr = getCurrentCenterRaDec();
    if (isHorizonFixed.value) {
        viewRa.value = ctr.ra;
        viewDec.value = ctr.dec;
    } else {
        syncHorizFromRaDec(ctr.ra, ctr.dec);
    }
    if (!fovPinned.value && hasValidSensorFov.value) {
        syncUnpinnedFovToCenter();
        emitSensorCenterIfChanged(true);
    }
    updateStatusReadout(ctr.ra, ctr.dec, true);
};

const onMouseUp = (e) => {
    const wasPanning = isPanning.value;
    const wasFovDrag = panDragKind === 'fov';
    isPanning.value = false;
    isDraggingFov.value = false;
    if (wasPanning && !panMoved) {
        if (canvasRectValid || refreshCanvasRect()) {
            const hit = findHitItem(e.clientX - canvasLeft, e.clientY - canvasTop);
            if (hit && (!wasFovDrag || hit.kind !== 'active' || isFovDetached.value)) {
                handleItemClick({ ...hit });
                return;
            }
        }
        if (wasFovDrag) {
            scheduleDraw();
        }
    } else if (wasPanning) {
        finishPanSync();
        scheduleDraw();
    }
};

const onMouseLeave = () => {
    const wasPanning = isPanning.value;
    const hadHoverFov = isHoveringFov.value;
    isPanning.value = false;
    isDraggingFov.value = false;
    isHoveringFov.value = false;
    pendingPointer = null;
    if (wasPanning) {
        finishPanSync();
    } else {
        const ctr = getCurrentCenterRaDec();
        updateStatusReadout(ctr.ra, ctr.dec, true);
    }
    if (hoveredItem.value) {
        hoveredItem.value = null;
        scheduleDraw();
    } else if (wasPanning || hadHoverFov) {
        scheduleDraw();
    }
};

const getTouchDist = (t1, t2) => Math.hypot(t2.clientX - t1.clientX, t2.clientY - t1.clientY);

const onTouchStart = (e) => {
    refreshCanvasRect();
    if (e.touches.length === 2) {
        isPanning.value = false;
        isDraggingFov.value = false;
        pinchStartDist = getTouchDist(e.touches[0], e.touches[1]);
        pinchStartFov = mapFovDeg.value;
    } else if (e.touches.length === 1) {
        const t = e.touches[0];
        beginPanGesture(t.clientX, t.clientY, false);
    }
};

const onTouchMove = (e) => {
    if (e.touches.length === 2 && pinchStartDist > 0) {
        const dist = getTouchDist(e.touches[0], e.touches[1]);
        const scale = pinchStartDist / Math.max(dist, 10);
        const targetFov = pinchStartFov * scale;
        applyZoomFactor(targetFov / mapFovDeg.value);
        e.preventDefault();
        return;
    }
    if (isPanning.value && e.touches.length === 1) {
        const t = e.touches[0];
        pendingPointer = {
            px: canvasRectValid ? t.clientX - canvasLeft : 0,
            py: canvasRectValid ? t.clientY - canvasTop : 0,
            clientX: t.clientX,
            clientY: t.clientY
        };
        if (pointerRafId === null) {
            pointerRafId = requestAnimationFrame(flushPointerFrame);
        }
        e.preventDefault();
    }
};

const onTouchEnd = (e) => {
    if (e.touches.length === 0) {
        const wasPanning = isPanning.value;
        isPanning.value = false;
        isDraggingFov.value = false;
        pinchStartDist = 0;
        if (wasPanning) {
            finishPanSync();
        } else {
            const ctr = getCurrentCenterRaDec();
            updateStatusReadout(ctr.ra, ctr.dec, true);
        }
        scheduleDraw();
    }
};

const onKeyDown = (e) => {
    if (e.key === 'Shift') {
        isShiftHeld.value = true;
    } else if (e.key === 'Escape') {
        emit('close');
    }
};

const onKeyUp = (e) => {
    if (e.key === 'Shift') {
        isShiftHeld.value = false;
    }
};

const onWindowBlur = () => {
    isShiftHeld.value = false;
};

// --- LIGHTWEIGHT WATCHERS & LIFECYCLE ---
watch(
    () => [
        props.objects?.length,
        skyAtlasItems.value.length,
        skyAtlasSectorMode.value
    ],
    () => {
        rebuildSceneIndex();
        scheduleDraw();
    },
    { immediate: true }
);

watch(
    () => props.object?.name,
    (newName, oldName) => {
        if (newName && newName !== oldName) {
            if (props.object?.image_url) {
                upsertLivePhotoFromObject(props.object);
            }
            const tgt = getCatalogTargetCoords();
            fovRa.value = tgt.ra;
            fovDec.value = tgt.dec;
            lastEmittedFov = { ra: tgt.ra, dec: tgt.dec };
            viewRa.value = tgt.ra;
            viewDec.value = tgt.dec;
            syncHorizFromRaDec(tgt.ra, tgt.dec);
            updateStatusReadout(tgt.ra, tgt.dec, true);
            scheduleDraw();
        }
    }
);

watch(
    () => [props.sensorCenter?.ra, props.sensorCenter?.dec],
    ([sRa, sDec]) => {
        if (isDraggingFov.value) return;
        const pRa = parseFloat(sRa);
        const pDec = parseFloat(sDec);
        if (!Number.isFinite(pRa) || !Number.isFinite(pDec)) return;
        const nextRa = wrapRaDeg(pRa);
        const nextDec = clampDecDeg(pDec);
        if (
            Math.abs(nextRa - lastEmittedFov.ra) < 1e-5 &&
            Math.abs(nextDec - lastEmittedFov.dec) < 1e-5
        ) {
            return;
        }
        fovRa.value = nextRa;
        fovDec.value = nextDec;
        lastEmittedFov = { ra: nextRa, dec: nextDec };
        if (!fovPinned.value) {
            viewRa.value = nextRa;
            viewDec.value = nextDec;
            syncHorizFromRaDec(nextRa, nextDec, effectiveTimeMs.value);
        }
        scheduleDraw();
    }
);

watch(
    () => [
        props.object?.image_url,
        props.object?.status,
        props.rotation,
        props.sensorFov?.w,
        props.sensorFov?.h,
        props.comparisonRectangles?.length,
        showCachedPhotos.value,
        showConstellations.value,
        showGrid.value,
        showDsos.value,
        applyTargetFilters.value,
        filteredNameSet.value,
        showHorizon.value,
        observerLocation.value?.lat,
        observerLocation.value?.lon,
        minAltitudeDeg.value
    ],
    ([newUrl, newStatus]) => {
        if (newUrl || newStatus === 'cached') {
            upsertLivePhotoFromObject(props.object);
        }
        if (
            applyTargetFilters.value &&
            hoveredItem.value &&
            !filteredNameSet.value.has(hoveredItem.value.name)
        ) {
            hoveredItem.value = null;
        }
        if (!viewHorizSynced && observerLocation.value) {
            syncHorizFromRaDec(viewRa.value, viewDec.value);
        }
        updateStatusReadout(lastReadoutCoords.ra, lastReadoutCoords.dec, true);
        scheduleDraw();
    }
);

watch(
    () => props.initialMapFov,
    (val) => {
        if (Number.isFinite(val) && val > 0) {
            mapFovDeg.value = Math.max(0.4, Math.min(100.0, val));
            scheduleDraw();
        }
    }
);

watch(fovCtrlRef, (el) => {
    if (el && fovOverlayVisible.value) {
        el.style.transform = `translate3d(${fovOverlayLeft}px, ${fovOverlayTop}px, 0)`;
    }
});

watch(
    () => [props.isLiveObsTime, props.liveNowMs, props.effectiveObsTimeMs],
    ([nextLive, nextNow, nextEff]) => {
        let changed = false;
        if (typeof nextLive === 'boolean' && nextLive !== isLiveTime.value) {
            isLiveTime.value = nextLive;
            changed = true;
        }
        if (Number.isFinite(nextNow) && Math.abs(nextNow - liveNowMs.value) >= 1) {
            liveNowMs.value = nextNow;
            if (isLiveTime.value) changed = true;
        }
        if (
            !isLiveTime.value &&
            Number.isFinite(nextEff) &&
            Math.abs(nextEff - scrubTimeMs.value) >= 1
        ) {
            scrubTimeMs.value = nextEff;
            changed = true;
        }
        if (changed) {
            refreshReadoutAfterTimeChange();
            if ((showHorizon.value || isHorizonFixed.value) && observerLocation.value) {
                scheduleDraw();
            }
        }
    }
);

watch(skyAtlasVersion, () => {
    scheduleDraw();
});

onMounted(async () => {
    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);
    window.addEventListener('blur', onWindowBlur);
    window.addEventListener('resize', refreshCanvasRect, { passive: true });
    window.addEventListener('scroll', refreshCanvasRect, { capture: true, passive: true });

    if (props.liveNowMs === undefined) {
        liveClockTimer = window.setInterval(() => {
            if (isLiveTime.value) {
                liveNowMs.value = Date.now();
                refreshReadoutAfterTimeChange();
                if ((showHorizon.value || isHorizonFixed.value) && observerLocation.value) {
                    scheduleDraw();
                }
            }
        }, 30000);
    }

    if (containerRef.value) {
        const updateDims = () => {
            if (!containerRef.value) return;
            viewportW.value = containerRef.value.clientWidth || 800;
            viewportH.value = containerRef.value.clientHeight || 600;
            refreshCanvasRect();
            scheduleDraw();
        };
        resizeObserver = new ResizeObserver(updateDims);
        resizeObserver.observe(containerRef.value);
        updateDims();
    }

    const hasAtlas = await ensureSkyAtlasLoaded();
    if (!hasAtlas || !skyAtlasSectorMode.value) {
        fetchCachedSkyImages();
    }
    await loadSkyData();
    isLoadingSky.value = false;
    refreshCanvasRect();
    const ctr = getCurrentCenterRaDec();
    updateStatusReadout(ctr.ra, ctr.dec, true);
    scheduleDraw();
});

onUnmounted(() => {
    if (isFovDetached.value) {
        emitSensorCenterIfChanged();
    }
    window.removeEventListener('keydown', onKeyDown);
    window.removeEventListener('keyup', onKeyUp);
    window.removeEventListener('blur', onWindowBlur);
    window.removeEventListener('resize', refreshCanvasRect);
    window.removeEventListener('scroll', refreshCanvasRect, { capture: true });
    if (liveClockTimer) clearInterval(liveClockTimer);
    if (resizeObserver) resizeObserver.disconnect();
    if (rafId) cancelAnimationFrame(rafId);
    if (pointerRafId) cancelAnimationFrame(pointerRafId);
    if (animFrameId) cancelAnimationFrame(animFrameId);
});
</script>

<template>
    <div
        class="sky-context-root"
        ref="containerRef"
        :class="{
            'is-panning': isPanning && !isDraggingFov,
            'is-dragging-fov': isDraggingFov,
            'is-hovering-fov': isHoveringFov && !isDraggingFov,
            'has-hover-hit': Boolean(hoveredItem) && !isHoveringFov
        }"
        @mousedown="onMouseDown"
        @mousemove="onMouseMove"
        @mouseup="onMouseUp"
        @dblclick="onDoubleClick"
        @mouseleave="onMouseLeave"
        @wheel.prevent="onWheel"
        @touchstart="onTouchStart"
        @touchmove="onTouchMove"
        @touchend="onTouchEnd"
        @touchcancel="onTouchEnd"
    >
        <canvas ref="canvasRef" class="sky-context-canvas"></canvas>

        <!-- Contextual Pin / Unpin & Reset Controls Anchored Next to Sensor FOV Window -->
        <div
            v-if="hasValidSensorFov"
            v-show="fovOverlayVisible"
            ref="fovCtrlRef"
            class="sky-fov-floating-ctrl"
            @mousedown.stop
            @mouseup.stop
            @dblclick.stop
            @touchstart.stop
            @touchend.stop
        >
            <button
                type="button"
                class="fov-anchor-btn pin-btn"
                :class="{ active: fovPinned }"
                @click="toggleFovPin"
                :aria-pressed="fovPinned"
                :title="
                    fovPinned
                        ? 'FOV pinned to sky: moves and zooms with the sky (drag frame to reposition) · Click to unpin & lock to view center'
                        : 'FOV unpinned: stays locked in the center of the view while panning or zooming · Click to pin to current sky coordinates'
                "
            >
                <svg
                    viewBox="0 0 16 16"
                    width="13"
                    height="13"
                    fill="none"
                    aria-hidden="true"
                    class="fov-pin-svg"
                >
                    <path
                        d="M5.2 2.2h5.6l-.7 3.5 1.9 1.8v1.2H4V7.5l1.9-1.8-.7-3.5Z"
                        fill="currentColor"
                        fill-opacity="0.28"
                        stroke="currentColor"
                        stroke-width="1.35"
                        stroke-linejoin="round"
                    />
                    <path
                        d="M8 8.7v5.1"
                        stroke="currentColor"
                        stroke-width="1.5"
                        stroke-linecap="round"
                    />
                </svg>
            </button>

            <button
                v-if="isFovDetached"
                type="button"
                class="fov-anchor-btn reset-btn"
                @click="resetSensorFovToTarget"
                title="Reset sensor FOV back onto active target"
            >
                ↺
            </button>
        </div>

        <!-- Top-Left Navigation & Off-Screen FOV Recovery -->
        <div class="sky-top-left-bar" @mousedown.stop @touchstart.stop>
            <button
                type="button"
                class="sky-pill-btn primary-back"
                @click="emit('close')"
                title="Return to close-up Framing image view (Esc)"
            >
                <span class="pill-arrow">‹</span>
                <span>Framing</span>
            </button>

            <button
                type="button"
                class="sky-pill-btn"
                :class="{ 'highlight-center': isOffTarget }"
                @click="centerOnTarget"
                title="Centre Sky Map on active target"
            >
                <span class="pill-icon">⌖</span>
                <span>Centre on target</span>
            </button>

            <button
                v-if="hasValidSensorFov && fovPinned && !fovOverlayVisible"
                type="button"
                class="sky-pill-btn fov-ctrl-btn unpin-fov-btn"
                @click="toggleFovPin"
                title="Pinned sensor FOV is off-screen · Click to unpin and bring it to the center of the view"
            >
                <span>⌖ Unpin FOV (center)</span>
            </button>

            <button
                v-if="hasValidSensorFov && isFovDetached && !fovOverlayVisible"
                type="button"
                class="sky-pill-btn fov-ctrl-btn reset-fov-btn"
                @click="resetSensorFovToTarget"
                title="Sensor FOV is currently off-screen · Click to snap it back onto the active target"
            >
                <span>↺ Reset FOV</span>
            </button>
        </div>

        <!-- Top-Right Layer Toggles & Quick Zoom -->
        <div class="sky-top-right-bar" @mousedown.stop @touchstart.stop>
            <button
                v-if="observerLocation"
                type="button"
                class="sky-layer-chip horizon-chip"
                :class="{ active: showHorizon }"
                @click="toggleHorizonLayer"
                :title="`Toggle local horizon, cardinal directions & min altitude (${observerLocation.coordsLabel})`"
            >
                <span>⛰ Horizon</span>
            </button>
            <button
                v-if="observerLocation"
                type="button"
                class="sky-layer-chip frame-lock-chip"
                :class="{ active: isHorizonFixed }"
                @click="toggleSkyFrameMode"
                :title="
                    isHorizonFixed
                        ? 'Horizon Fixed (Alt-Az): sky moves with time & drags along the level horizon · Click for Sky Fixed (EQ)'
                        : 'Sky Fixed (Equatorial): horizon moves with time · Click to lock Horizon Fixed (Alt-Az)'
                "
            >
                <span>{{ isHorizonFixed ? '🧭 Horizon Fixed' : '✦ Sky Fixed' }}</span>
            </button>
            <button
                type="button"
                class="sky-layer-chip"
                :class="{ active: showCachedPhotos }"
                @click="showCachedPhotos = !showCachedPhotos"
                title="Toggle pre-composited sky survey map at true celestial coordinates"
            >
                <span v-if="skyAtlasSectorMode">📷 Sky Survey</span>
                <span v-else>📷 Photos ({{ skyPhotoCount }})</span>
            </button>
            <button
                type="button"
                class="sky-layer-chip"
                :class="{ active: showConstellations }"
                @click="showConstellations = !showConstellations"
                title="Toggle constellation lines, boundaries, and names"
            >
                <span>✦ Constellations</span>
            </button>
            <button
                type="button"
                class="sky-layer-chip"
                :class="{ active: showDsos }"
                @click="showDsos = !showDsos"
                title="Toggle nearby deep-sky object markers"
            >
                <span>◎ DSOs</span>
            </button>
            <button
                type="button"
                class="sky-layer-chip filter-targets-chip"
                :class="{ active: applyTargetFilters }"
                @click="toggleApplyTargetFilters"
                :title="
                    applyTargetFilters
                        ? `Showing only the ${filteredTargetCount} targets in your current filtered list · Click to show all catalog DSOs`
                        : `Restrict Sky Map markers to the ${filteredTargetCount} targets in your current filtered list`
                "
            >
                <span>⧩ Apply Filters ({{ filteredTargetCount }})</span>
            </button>
            <button
                type="button"
                class="sky-layer-chip"
                :class="{ active: showGrid }"
                @click="showGrid = !showGrid"
                title="Toggle celestial RA/Dec coordinate grid"
            >
                <span>⊞ Grid</span>
            </button>

            <div class="sky-zoom-stepper">
                <button
                    type="button"
                    class="sky-step-btn"
                    @click="applyZoomFactor(1 / 1.35)"
                    title="Zoom In"
                >
                    +
                </button>
                <button
                    type="button"
                    class="sky-step-btn"
                    @click="applyZoomFactor(1.35)"
                    title="Zoom Out"
                >
                    −
                </button>
            </div>
        </div>

        <!-- Loading Indicator -->
        <div v-if="isLoadingSky" class="sky-loading-badge">
            <span>Loading star chart...</span>
        </div>

        <!-- Bottom-Left Live Coordinate / Alt-Az / Constellation / FOV Readout -->
        <div class="sky-bottom-left-bar">
            <span class="coord-seg">RA {{ readoutRaText }}</span>
            <span class="coord-seg">Dec {{ readoutDecText }}</span>
            <span
                v-if="readoutAltText"
                class="coord-seg alt-seg"
                :class="{ 'below-horizon': readoutBelowHorizon }"
            >
                Alt {{ readoutAltText }}
            </span>
            <span v-if="readoutAzText" class="coord-seg az-seg">
                Az {{ readoutAzText }}
            </span>
            <span v-if="readoutConstellation" class="coord-seg const-seg">
                {{ readoutConstellation }}
            </span>
            <span class="coord-seg fov-seg">FOV {{ readoutFovText }}</span>
        </div>

        <!-- Bottom-Right Observation Time & Date Bar (when Horizon is enabled) -->
        <ObservingDateConsole
            v-if="showHorizon && observerLocation"
            variant="sky"
            class="sky-time-bar"
            :is-live-time="isLiveTime"
            :live-now-ms="liveNowMs"
            :effective-time-ms="effectiveTimeMs"
            :object="object"
            :settings="settings"
            :night-times="nightTimes"
            @update-time="handleConsoleTimeUpdate"
        >
            <template #header>
                <div class="time-frame-row">
                    <div class="frame-mode-seg" role="group" aria-label="Sky reference frame">
                        <button
                            type="button"
                            class="frame-seg-btn"
                            :class="{ active: !isHorizonFixed }"
                            @click="setSkyFrameMode('sky')"
                            title="Sky Fixed (Equatorial, North up): sky stays fixed while the horizon moves with time"
                        >
                            ✦ Sky Fixed
                        </button>
                        <button
                            type="button"
                            class="frame-seg-btn"
                            :class="{ active: isHorizonFixed }"
                            @click="setSkyFrameMode('horizon')"
                            title="Horizon Fixed (Alt-Az, Zenith up): horizon stays fixed & level while the sky moves with time. Drag horizontally to scan along the horizon."
                        >
                            ⛰ Horizon Fixed
                        </button>
                    </div>

                    <button
                        type="button"
                        class="drag-sky-pill"
                        :class="{ active: isTimeDragActive }"
                        @click="toggleDragTimeMode"
                        :title="
                            isTimeDragActive
                                ? 'Drag Sky active: dragging advances/rewinds sky time across the fixed horizon · Click to return to panning along the horizon'
                                : 'Drag the sky through time across a fixed horizon & compass (or hold Shift + drag in Horizon Fixed mode)'
                        "
                    >
                        <span>⟳ Drag Sky</span>
                    </button>
                </div>
            </template>
        </ObservingDateConsole>
    </div>
</template>

<style scoped>
.sky-context-root {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    overflow: hidden;
    background: #04060c;
    cursor: grab;
    user-select: none;
    touch-action: none;
    contain: strict;
    z-index: 28;
}

.sky-context-root.is-panning,
.sky-context-root.is-dragging-fov {
    cursor: grabbing;
}

.sky-context-root.is-hovering-fov {
    cursor: move;
}

.sky-context-root.has-hover-hit:not(.is-panning):not(.is-dragging-fov):not(.is-hovering-fov) {
    cursor: pointer;
}

.sky-context-canvas {
    display: block;
    width: 100%;
    height: 100%;
}

/* Top-Left Pill Buttons (no backdrop-filter over 60fps canvas to avoid GPU readback stalls) */
.sky-top-left-bar {
    position: absolute;
    top: 12px;
    left: 12px;
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    max-width: calc(100% - 24px);
    z-index: 32;
}

/* Contextual Floating Pin & Reset Controls Beside Sensor FOV Window (GPU translate3d, zero backdrop-filter) */
.sky-fov-floating-ctrl {
    position: absolute;
    top: 0;
    left: 0;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    z-index: 26;
    will-change: transform;
    pointer-events: auto;
}

.fov-anchor-btn {
    width: 26px;
    height: 26px;
    padding: 0;
    margin: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 6px;
    background: rgba(11, 16, 26, 0.92);
    border: 1px solid rgba(245, 158, 11, 0.45);
    color: #fde68a;
    font-size: 0.85rem;
    font-weight: 700;
    line-height: 1;
    cursor: pointer;
    box-shadow: 0 3px 10px rgba(0, 0, 0, 0.55);
    transition: background-color 0.12s ease, border-color 0.12s ease, color 0.12s ease;
}

.fov-anchor-btn:hover {
    background: rgba(28, 36, 52, 0.97);
    border-color: #fbbf24;
    color: #ffffff;
}

.fov-anchor-btn.pin-btn .fov-pin-svg {
    transform: rotate(28deg);
    transition: transform 0.14s ease;
}

.fov-anchor-btn.pin-btn.active {
    background: rgba(10, 46, 48, 0.96);
    border-color: #2dd4bf;
    color: #5eead4;
    box-shadow: 0 0 0 1px rgba(45, 212, 191, 0.28), 0 3px 10px rgba(0, 0, 0, 0.6);
}

.fov-anchor-btn.pin-btn.active:hover {
    background: rgba(14, 62, 64, 0.98);
    border-color: #5eead4;
    color: #ccfbf1;
}

.fov-anchor-btn.pin-btn.active .fov-pin-svg {
    transform: rotate(0deg);
}

.fov-anchor-btn.reset-btn {
    background: rgba(28, 22, 14, 0.93);
    border-color: rgba(251, 191, 36, 0.48);
    color: #fcd34d;
}

.fov-anchor-btn.reset-btn:hover {
    background: rgba(50, 36, 16, 0.97);
    border-color: #fbbf24;
    color: #fef3c7;
}

.sky-pill-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 6px 13px;
    margin: 0;
    border-radius: 7px;
    background: rgba(11, 16, 26, 0.92);
    border: 1px solid rgba(148, 163, 184, 0.28);
    color: #e2e8f0;
    font-size: 0.8rem;
    font-weight: 500;
    line-height: 1.2;
    cursor: pointer;
    transition: background-color 0.12s ease, border-color 0.12s ease, color 0.12s ease;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.45);
}

.sky-pill-btn:hover {
    background: rgba(24, 33, 50, 0.96);
    border-color: rgba(148, 163, 184, 0.5);
    color: #ffffff;
}

.sky-pill-btn.primary-back {
    border-color: rgba(56, 189, 248, 0.42);
}

.sky-pill-btn.primary-back:hover {
    border-color: #38bdf8;
    background: rgba(14, 70, 96, 0.92);
}

.sky-pill-btn.highlight-center {
    border-color: rgba(245, 158, 11, 0.55);
    color: #fde68a;
}

.sky-pill-btn.fov-ctrl-btn {
    padding: 5px 10px;
    font-size: 0.74rem;
    color: #cbd5e1;
}

.sky-pill-btn.unpin-fov-btn {
    background: rgba(12, 36, 40, 0.92);
    border-color: rgba(45, 212, 191, 0.45);
    color: #5eead4;
}

.sky-pill-btn.unpin-fov-btn:hover {
    background: rgba(16, 54, 58, 0.96);
    border-color: #2dd4bf;
    color: #ccfbf1;
}

.sky-pill-btn.reset-fov-btn {
    background: rgba(28, 22, 14, 0.92);
    border-color: rgba(251, 191, 36, 0.45);
    color: #fcd34d;
}

.sky-pill-btn.reset-fov-btn:hover {
    background: rgba(48, 34, 16, 0.96);
    border-color: #fbbf24;
    color: #fef3c7;
}

.pill-arrow {
    font-size: 1.05rem;
    font-weight: 700;
    line-height: 1;
}

.pill-icon {
    font-size: 0.92rem;
    line-height: 1;
}

/* Top-Right Layer Chips & Zoom Stepper */
.sky-top-right-bar {
    position: absolute;
    top: 12px;
    right: 12px;
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 6px;
    z-index: 32;
}

.sky-layer-chip {
    padding: 5px 10px;
    margin: 0;
    border-radius: 7px;
    background: rgba(11, 16, 26, 0.9);
    border: 1px solid rgba(148, 163, 184, 0.22);
    color: #94a3b8;
    font-size: 0.74rem;
    font-weight: 500;
    line-height: 1.2;
    cursor: pointer;
    transition: background-color 0.12s ease, border-color 0.12s ease, color 0.12s ease;
}

.sky-layer-chip:hover {
    color: #e2e8f0;
    border-color: rgba(148, 163, 184, 0.42);
}

.sky-layer-chip.active {
    background: rgba(14, 32, 54, 0.94);
    border-color: rgba(56, 189, 248, 0.48);
    color: #e0f2fe;
}

.sky-layer-chip.horizon-chip.active {
    background: rgba(8, 38, 32, 0.94);
    border-color: rgba(52, 211, 153, 0.52);
    color: #d1fae5;
}

.sky-layer-chip.frame-lock-chip.active {
    background: rgba(12, 42, 46, 0.95);
    border-color: rgba(45, 212, 191, 0.58);
    color: #ccfbf1;
}

.sky-layer-chip.filter-targets-chip.active {
    background: rgba(46, 26, 56, 0.95);
    border-color: rgba(192, 132, 252, 0.65);
    color: #f3e8ff;
}

.sky-zoom-stepper {
    display: inline-flex;
    border-radius: 7px;
    overflow: hidden;
    border: 1px solid rgba(148, 163, 184, 0.28);
    background: rgba(11, 16, 26, 0.92);
}

.sky-step-btn {
    width: 28px;
    height: 27px;
    padding: 0;
    margin: 0;
    border: none;
    background: transparent;
    color: #e2e8f0;
    font-size: 0.95rem;
    font-weight: 600;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
}

.sky-step-btn + .sky-step-btn {
    border-left: 1px solid rgba(148, 163, 184, 0.22);
}

.sky-step-btn:hover {
    background: rgba(56, 189, 248, 0.22);
    color: #ffffff;
}

.sky-loading-badge {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    padding: 8px 16px;
    border-radius: 8px;
    background: rgba(10, 15, 28, 0.92);
    border: 1px solid rgba(148, 163, 184, 0.25);
    color: #cbd5e1;
    font-size: 0.82rem;
    pointer-events: none;
}

/* Bottom-Left Status Readout (RA / Dec / Alt / Az / Constellation / FOV) */
.sky-bottom-left-bar {
    position: absolute;
    bottom: 10px;
    left: 12px;
    display: inline-flex;
    align-items: center;
    gap: 13px;
    padding: 4px 10px;
    border-radius: 6px;
    background: rgba(6, 9, 16, 0.88);
    color: #cbd5e1;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.75rem;
    letter-spacing: 0.01em;
    pointer-events: none;
    z-index: 32;
}

.alt-seg {
    color: #6ee7b7;
}

.alt-seg.below-horizon {
    color: #fca5a5;
}

.az-seg {
    color: #94a3b8;
}

.const-seg {
    font-family: system-ui, -apple-system, sans-serif;
    color: #e2e8f0;
    font-weight: 500;
}

.fov-seg {
    color: #93c5fd;
}

/* Bottom-Right Observation Time & Date Bar */
.sky-time-bar {
    position: absolute;
    bottom: 10px;
    right: 12px;
    z-index: 33;
}

.time-frame-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
}

.frame-mode-seg {
    display: inline-flex;
    align-items: center;
    flex: 1;
    min-width: 0;
    border-radius: 5px;
    overflow: hidden;
    border: 1px solid rgba(148, 163, 184, 0.26);
    background: rgba(13, 19, 31, 0.94);
}

.frame-seg-btn {
    flex: 1;
    padding: 2.5px 7px;
    margin: 0;
    border: none;
    background: transparent;
    color: #94a3b8;
    font-size: 0.68rem;
    font-weight: 500;
    line-height: 1.25;
    white-space: nowrap;
    cursor: pointer;
    transition: background-color 0.12s ease, color 0.12s ease;
}

.frame-seg-btn + .frame-seg-btn {
    border-left: 1px solid rgba(148, 163, 184, 0.2);
}

.frame-seg-btn:hover {
    color: #e2e8f0;
    background: rgba(30, 41, 59, 0.72);
}

.frame-seg-btn.active {
    background: rgba(14, 48, 66, 0.95);
    color: #e0f2fe;
    font-weight: 600;
}

.frame-seg-btn + .frame-seg-btn.active {
    background: rgba(9, 48, 40, 0.95);
    color: #d1fae5;
}

.drag-sky-pill {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2.5px 7px;
    margin: 0;
    border-radius: 5px;
    border: 1px solid rgba(148, 163, 184, 0.26);
    background: rgba(16, 24, 38, 0.92);
    color: #cbd5e1;
    font-size: 0.68rem;
    font-weight: 500;
    line-height: 1.25;
    white-space: nowrap;
    flex-shrink: 0;
    cursor: pointer;
    transition: background-color 0.12s ease, border-color 0.12s ease, color 0.12s ease;
}

.drag-sky-pill:hover {
    background: rgba(28, 39, 58, 0.96);
    border-color: rgba(148, 163, 184, 0.48);
    color: #ffffff;
}

.drag-sky-pill.active {
    background: rgba(54, 34, 10, 0.95);
    border-color: rgba(245, 158, 11, 0.68);
    color: #fde68a;
    font-weight: 600;
}

@media (max-width: 1180px) {
    .sky-top-right-bar {
        top: 50px;
        right: 12px;
        max-width: calc(100% - 24px);
    }
}

@media (max-width: 960px) {
    .sky-time-bar {
        bottom: 42px;
        right: 12px;
        left: 12px;
        max-width: none;
    }
}

@media (max-width: 768px) {
    .sky-top-right-bar {
        top: 86px;
        left: 12px;
        right: auto;
        justify-content: flex-start;
    }

    .sky-bottom-left-bar {
        gap: 8px;
        font-size: 0.68rem;
        right: 12px;
        flex-wrap: wrap;
    }

    .sky-time-bar {
        bottom: 52px;
        min-width: 0;
    }
}
</style>
