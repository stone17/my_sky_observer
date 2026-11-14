<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue';
import {
    skyAtlasVersion,
    skyAtlasItems,
    ensureSkyAtlasLoaded,
    ensureAtlasSheetImage
} from '../utils/skyAtlas.js';

const props = defineProps({
    object: { type: Object, default: null },
    objects: { type: Array, default: () => [] },
    settings: { type: Object, default: () => ({}) },
    clientSettings: { type: Object, default: () => ({}) },
    nightTimes: { type: Object, default: () => ({}) },
    sensorFov: { type: Object, default: () => ({ w: 0, h: 0 }) },
    comparisonRectangles: { type: Array, default: () => [] },
    sensorCenter: { type: Object, default: null },
    rotation: { type: Number, default: 0 },
    baseImageFov: { type: Number, default: 1.0 },
    initialMapFov: { type: Number, default: 35.0 }
});

const emit = defineEmits(['close', 'select-object']);

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

const wrapRaDeg = (ra) => {
    let r = Number(ra) % 360.0;
    if (r < 0) r += 360.0;
    return r;
};

const clampDecDeg = (dec) => Math.max(-89.99, Math.min(89.99, Number(dec)));

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

const SHORT_WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const SHORT_MONTHS = [
    'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'
];

const formatLocalClock = (ms) => {
    const dt = new Date(ms);
    const hh = String(dt.getHours()).padStart(2, '0');
    const mm = String(dt.getMinutes()).padStart(2, '0');
    return `${hh}:${mm}`;
};

const formatDateInputYmd = (ms) => {
    const dt = new Date(ms);
    const y = dt.getFullYear();
    const m = String(dt.getMonth() + 1).padStart(2, '0');
    const d = String(dt.getDate()).padStart(2, '0');
    return `${y}-${m}-${d}`;
};

const formatObsDateBadge = (ms, refNowMs) => {
    const dt = new Date(ms);
    const ref = new Date(refNowMs);
    const mon = SHORT_MONTHS[dt.getMonth()];
    const day = dt.getDate();
    if (dt.getFullYear() !== ref.getFullYear()) {
        return `${mon} ${day}, ${dt.getFullYear()}`;
    }
    const wk = SHORT_WEEKDAYS[dt.getDay()];
    return `${wk}, ${mon} ${day}`;
};

const getObservingEveningDate = (ms) => {
    const dt = new Date(ms);
    const eve = new Date(
        dt.getFullYear(),
        dt.getMonth(),
        dt.getHours() < 12 ? dt.getDate() - 1 : dt.getDate(),
        12,
        0,
        0,
        0
    );
    const year = eve.getFullYear();
    const month0 = eve.getMonth();
    const day = eve.getDate();
    const key = `${year}-${String(month0 + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
    return { year, month0, day, key };
};

const computeLstDeg = (timeMs, lonDeg) => {
    const jd = timeMs / 86400000.0 + 2440587.5;
    const d = jd - 2451545.0;
    const T = d / 36525.0;
    const gmst =
        280.46061837 +
        360.98564736629 * d +
        0.000387933 * T * T -
        (T * T * T) / 38710000.0;
    return wrapRaDeg(gmst + lonDeg);
};

const computeSolarEquatorial = (timeMs) => {
    const d = timeMs / 86400000.0 + 2440587.5 - 2451545.0;
    const g = (357.52911 + 0.98560028 * d) * DEG2RAD;
    const q = 280.46646 + 0.98564736 * d;
    const lam = (q + 1.914602 * Math.sin(g) + 0.019993 * Math.sin(2.0 * g)) * DEG2RAD;
    const eps = (23.439291 - 0.000013 * d) * DEG2RAD;
    const sinLam = Math.sin(lam);
    const raDeg = wrapRaDeg(Math.atan2(Math.cos(eps) * sinLam, Math.cos(lam)) * RAD2DEG);
    const sinDec = Math.max(-1.0, Math.min(1.0, Math.sin(eps) * sinLam));
    const cosDec = Math.sqrt(Math.max(0.0, 1.0 - sinDec * sinDec));
    return { raDeg, sinDec, cosDec };
};

const nightEphemerisCache = new Map();

const computeNightEphemerisForEvening = (year, month0, day, latDeg, lonDeg) => {
    const cacheKey = `${year}-${month0}-${day}:${latDeg.toFixed(3)}:${lonDeg.toFixed(3)}`;
    const cached = nightEphemerisCache.get(cacheKey);
    if (cached) return cached;

    const clampedLat = Math.max(-89.8, Math.min(89.8, latDeg));
    const sinLat = Math.sin(clampedLat * DEG2RAD);
    const cosLat = Math.cos(clampedLat * DEG2RAD);

    // Refine true solar midnight (solar anti-transit, HA = 180°) for evening (year, month0, day)
    const approxMidMs = Date.UTC(year, month0, day, 24, 0, 0) - (lonDeg / 15.0) * 3600000.0;
    const sunMid = computeSolarEquatorial(approxMidMs);
    const lstMid = computeLstDeg(approxMidMs, lonDeg);
    let haErr = wrapRaDeg(lstMid - sunMid.raDeg - 180.0);
    if (haErr > 180.0) haErr -= 360.0;
    const solarMidMs = approxMidMs - (haErr / 360.0) * 86400000.0;

    // Solve evening & morning timestamps where Sun crosses altDeg around solarMidMs
    const solveCrossingPair = (altDeg) => {
        const sinAlt = Math.sin(altDeg * DEG2RAD);
        const cosH0 = (sinAlt - sinLat * sunMid.sinDec) / Math.max(1e-6, cosLat * sunMid.cosDec);
        if (cosH0 < -1.0) return { state: 'never_below', eveMs: null, mornMs: null };
        if (cosH0 > 1.0) return { state: 'always_below', eveMs: null, mornMs: null };

        const halfNightDeg0 = 180.0 - Math.acos(cosH0) * RAD2DEG;
        const guessOffsetMs = (halfNightDeg0 / 360.0) * 86400000.0;

        // 1-step refinement using solar RA/Dec at evening and morning crossing times
        const refineSide = (sign) => {
            const tGuess = solarMidMs + sign * guessOffsetMs;
            const sun = computeSolarEquatorial(tGuess);
            const cosH = (sinAlt - sinLat * sun.sinDec) / Math.max(1e-6, cosLat * sun.cosDec);
            if (cosH < -1.0 || cosH > 1.0) return tGuess;
            const halfNightDeg = 180.0 - Math.acos(cosH) * RAD2DEG;
            // Refine local solar anti-transit slightly for RA drift
            let dRa = sun.raDeg - sunMid.raDeg;
            if (dRa > 180.0) dRa -= 360.0;
            if (dRa < -180.0) dRa += 360.0;
            return solarMidMs + ((dRa + sign * halfNightDeg) / 360.0) * 86400000.0;
        };

        const eveMs = refineSide(-1.0);
        const mornMs = refineSide(1.0);
        if (!Number.isFinite(eveMs) || !Number.isFinite(mornMs) || mornMs <= eveMs) {
            return { state: 'never_below', eveMs: null, mornMs: null };
        }
        return { state: 'normal', eveMs, mornMs };
    };

    const horizonCross = solveCrossingPair(-0.833);
    const civilCross = solveCrossingPair(-6.0);
    const nautCross = solveCrossingPair(-12.0);
    const astroCross = solveCrossingPair(-18.0);

    let startMs;
    let endMs;
    let midMs = solarMidMs;
    let sunsetMs = horizonCross.eveMs;
    let sunriseMs = horizonCross.mornMs;

    if (horizonCross.state === 'normal') {
        startMs = sunsetMs - 30 * 60 * 1000;
        endMs = sunriseMs + 30 * 60 * 1000;
        if (astroCross.state === 'normal') {
            midMs = 0.5 * (astroCross.eveMs + astroCross.mornMs);
        } else {
            midMs = 0.5 * (sunsetMs + sunriseMs);
        }
    } else if (horizonCross.state === 'always_below') {
        // Polar winter night: 18h session window centered on solar midnight
        startMs = solarMidMs - 9 * 3600 * 1000;
        endMs = solarMidMs + 9 * 3600 * 1000;
        sunsetMs = startMs;
        sunriseMs = endMs;
    } else {
        // Polar midnight sun: 10h window centered on solar midnight
        startMs = solarMidMs - 5 * 3600 * 1000;
        endMs = solarMidMs + 5 * 3600 * 1000;
    }

    const sunAtMid = computeSolarEquatorial(solarMidMs);
    const midSinAlt = Math.max(
        -1.0,
        Math.min(1.0, sinLat * sunAtMid.sinDec - cosLat * sunAtMid.cosDec)
    );
    const midSunAltDeg = Math.asin(midSinAlt) * RAD2DEG;

    const result = {
        startMs,
        endMs,
        midMs,
        solarMidMs,
        midSunAltDeg,
        sunsetMs,
        sunriseMs,
        civilEveMs: civilCross.eveMs,
        civilMornMs: civilCross.mornMs,
        nautEveMs: nautCross.eveMs,
        nautMornMs: nautCross.mornMs,
        astroEveMs: astroCross.eveMs,
        astroMornMs: astroCross.mornMs
    };

    if (nightEphemerisCache.size > 128) {
        nightEphemerisCache.clear();
    }
    nightEphemerisCache.set(cacheKey, result);
    return result;
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

const getTargetCoords = () => {
    if (!props.object) return { ra: 83.822, dec: -5.391 };
    const ra =
        props.sensorCenter?.ra !== undefined
            ? props.sensorCenter.ra
            : props.object.center_ra !== undefined
              ? props.object.center_ra
              : props.object.ra;
    const dec =
        props.sensorCenter?.dec !== undefined
            ? props.sensorCenter.dec
            : props.object.center_dec !== undefined
              ? props.object.center_dec
              : props.object.dec;
    return {
        ra: wrapRaDeg(parseFloat(ra) || 0),
        dec: clampDecDeg(parseFloat(dec) || 0)
    };
};

const initialCoords = getTargetCoords();
const viewRa = ref(initialCoords.ra);
const viewDec = ref(initialCoords.dec);
const mapFovDeg = ref(Math.max(0.4, Math.min(100.0, Number(props.initialMapFov) || 35.0)));

// Layer visibility toggles
const showCachedPhotos = ref(true);
const showConstellations = ref(true);
const showGrid = ref(true);
const showDsos = ref(true);
const showHorizon = ref(true);

// Observer Location, Minimum Altitude & Observation Time State
const observerLocation = computed(() => {
    const loc = props.settings?.location;
    if (!loc) return null;
    const lat = Number(loc.latitude);
    const lon = Number(loc.longitude);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
    if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return null;

    const city = typeof loc.city_name === 'string' ? loc.city_name.trim() : '';
    const latTag = `${Math.abs(lat).toFixed(1)}°${lat >= 0 ? 'N' : 'S'}`;
    const lonTag = `${Math.abs(lon).toFixed(1)}°${lon >= 0 ? 'E' : 'W'}`;
    return {
        lat,
        lon,
        label: city || `${latTag} ${lonTag}`,
        coordsLabel: `${latTag}, ${lonTag}`
    };
});

const minAltitudeDeg = computed(() => {
    const raw = props.clientSettings?.min_hours_altitude ?? props.settings?.min_altitude ?? 30;
    const val = Number(raw);
    return Number.isFinite(val) ? Math.max(0, Math.min(88, val)) : 30;
});

const isLiveTime = ref(true);
const liveNowMs = ref(Date.now());
const scrubTimeMs = ref(Date.now());
const dateInputRef = ref(null);
const effectiveTimeMs = computed(() => (isLiveTime.value ? liveNowMs.value : scrubTimeMs.value));

const observingEvening = computed(() => {
    // When tracking live daytime (08:00..11:59), preview upcoming tonight's session on the slider
    if (isLiveTime.value) {
        const dt = new Date(liveNowMs.value);
        if (dt.getHours() >= 8) {
            const year = dt.getFullYear();
            const month0 = dt.getMonth();
            const day = dt.getDate();
            const key = `${year}-${String(month0 + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
            return { year, month0, day, key };
        }
    }
    return getObservingEveningDate(effectiveTimeMs.value);
});

const propsTonightEveningKey = computed(() => {
    const g0 = props.object?.altitude_graph?.[0]?.time;
    if (g0) {
        const t = Date.parse(g0);
        if (Number.isFinite(t)) return getObservingEveningDate(t).key;
    }
    const c0 = props.nightTimes?.civil?.[0];
    if (c0) {
        const t = Date.parse(c0);
        if (Number.isFinite(t)) return getObservingEveningDate(t).key;
    }
    const dt = new Date(liveNowMs.value);
    if (dt.getHours() >= 8) {
        return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')}`;
    }
    return getObservingEveningDate(liveNowMs.value).key;
});

const isTonightSession = computed(
    () => isLiveTime.value || observingEvening.value.key === propsTonightEveningKey.value
);

const activeNightEphemeris = computed(() => {
    const loc = observerLocation.value;
    if (!loc) return null;
    const eve = observingEvening.value;
    return computeNightEphemerisForEvening(eve.year, eve.month0, eve.day, loc.lat, loc.lon);
});

const sessionRange = computed(() => {
    if (isTonightSession.value) {
        // 1. Prefer object's tonight altitude_graph session bounds (sunset - 30m -> sunrise + 30m)
        const graph = props.object?.altitude_graph;
        if (Array.isArray(graph) && graph.length >= 2) {
            const s = Date.parse(graph[0].time);
            const e = Date.parse(graph[graph.length - 1].time);
            if (Number.isFinite(s) && Number.isFinite(e) && e > s) {
                let midMs = (s + e) * 0.5;
                const nightPair = props.nightTimes?.night;
                if (Array.isArray(nightPair) && nightPair.length === 2) {
                    const ns = Date.parse(nightPair[0]);
                    const ne = Date.parse(nightPair[1]);
                    if (Number.isFinite(ns) && Number.isFinite(ne) && ne > ns) {
                        midMs = (ns + ne) * 0.5;
                    }
                }
                return { startMs: s, endMs: e, midMs };
            }
        }

        // 2. Fallback to tonight's nightTimes twilight bounds
        const nt = props.nightTimes;
        if (nt && typeof nt === 'object') {
            let minT = Infinity;
            let maxT = -Infinity;
            for (const key of [
                'civil',
                'nautical',
                'astronomical',
                'night',
                'astronomical_morn',
                'nautical_morn',
                'civil_morn'
            ]) {
                const pair = nt[key];
                if (Array.isArray(pair) && pair.length === 2) {
                    const t0 = Date.parse(pair[0]);
                    const t1 = Date.parse(pair[1]);
                    if (Number.isFinite(t0) && t0 < minT) minT = t0;
                    if (Number.isFinite(t1) && t1 > maxT) maxT = t1;
                }
            }
            if (Number.isFinite(minT) && Number.isFinite(maxT) && maxT > minT) {
                const startMs = minT - 30 * 60 * 1000;
                const endMs = maxT + 30 * 60 * 1000;
                return { startMs, endMs, midMs: (startMs + endMs) * 0.5 };
            }
        }
    }

    // 3. Computed solar twilight bounds for any selected date & observer location
    const ephem = activeNightEphemeris.value;
    if (ephem) {
        return {
            startMs: ephem.startMs,
            endMs: ephem.endMs,
            midMs: ephem.midMs
        };
    }

    // 4. Clean local-clock fallback (17:00 -> 07:00 on observing evening)
    const eve = observingEvening.value;
    const startMs = new Date(eve.year, eve.month0, eve.day, 17, 0, 0, 0).getTime();
    const endMs = startMs + 14 * 3600 * 1000;
    return { startMs, endMs, midMs: (startMs + endMs) * 0.5 };
});

const sliderValueMs = computed(() => {
    const { startMs, endMs } = sessionRange.value;
    return Math.max(startMs, Math.min(endMs, effectiveTimeMs.value));
});

const sliderTrackStyle = computed(() => {
    const { startMs, endMs } = sessionRange.value;
    const span = endMs - startMs;
    if (span <= 0) {
        return {
            background:
                'linear-gradient(90deg, #334155 0%, #1e3a8a 25%, #0f172a 50%, #1e3a8a 75%, #334155 100%)'
        };
    }

    const DAY = 'rgba(71, 85, 105, 0.78)';
    const CIV = 'rgba(59, 130, 246, 0.46)';
    const NAU = 'rgba(37, 99, 235, 0.58)';
    const AST = 'rgba(29, 78, 216, 0.75)';
    const DRK = 'rgba(15, 23, 42, 0.96)';

    const pctMs = (t) =>
        Number.isFinite(t) ? Math.max(0, Math.min(100, ((t - startMs) / span) * 100)) : null;

    // Extract twilight transition timestamps from tonight's nightTimes or arbitrary-date solar ephemeris
    let s0 = null;
    let s1 = null;
    let c0 = null;
    let c1 = null;
    let n0 = null;
    let n1 = null;
    let a0 = null;
    let a1 = null;

    const nt = isTonightSession.value ? props.nightTimes : null;
    const ephem = activeNightEphemeris.value;

    if (nt && typeof nt === 'object' && (nt.civil || nt.nautical || nt.astronomical || nt.night)) {
        const parsePct = (iso) => (iso ? pctMs(Date.parse(iso)) : null);
        s0 = parsePct(nt.civil?.[0]);
        s1 = parsePct(nt.civil_morn?.[1]);
        c0 = parsePct(nt.nautical?.[0] ?? nt.civil?.[1]);
        c1 = parsePct(nt.nautical_morn?.[1] ?? nt.civil_morn?.[0]);
        n0 = parsePct(nt.astronomical?.[0] ?? (nt.night ? nt.nautical?.[1] : null));
        n1 = parsePct(nt.astronomical_morn?.[1] ?? (nt.night ? nt.nautical_morn?.[0] : null));
        a0 = parsePct(nt.night?.[0]);
        a1 = parsePct(nt.night?.[1]);
    } else if (ephem) {
        s0 = pctMs(ephem.sunsetMs);
        s1 = pctMs(ephem.sunriseMs);
        c0 = pctMs(ephem.civilEveMs);
        c1 = pctMs(ephem.civilMornMs);
        n0 = pctMs(ephem.nautEveMs);
        n1 = pctMs(ephem.nautMornMs);
        a0 = pctMs(ephem.astroEveMs);
        a1 = pctMs(ephem.astroMornMs);
    }

    if (s0 !== null && s1 !== null && s1 > s0) {
        const stops = [`${DAY} 0%`, `${DAY} ${s0.toFixed(1)}%`, `${CIV} ${s0.toFixed(1)}%`];
        if (c0 !== null && c1 !== null && c1 > c0) {
            stops.push(`${CIV} ${c0.toFixed(1)}%`, `${NAU} ${c0.toFixed(1)}%`);
            if (n0 !== null && n1 !== null && n1 > n0) {
                stops.push(`${NAU} ${n0.toFixed(1)}%`, `${AST} ${n0.toFixed(1)}%`);
                if (a0 !== null && a1 !== null && a1 > a0) {
                    stops.push(
                        `${AST} ${a0.toFixed(1)}%`,
                        `${DRK} ${a0.toFixed(1)}%`,
                        `${DRK} ${a1.toFixed(1)}%`,
                        `${AST} ${a1.toFixed(1)}%`
                    );
                }
                stops.push(`${AST} ${n1.toFixed(1)}%`, `${NAU} ${n1.toFixed(1)}%`);
            }
            stops.push(`${NAU} ${c1.toFixed(1)}%`, `${CIV} ${c1.toFixed(1)}%`);
        }
        stops.push(`${CIV} ${s1.toFixed(1)}%`, `${DAY} ${s1.toFixed(1)}%`, `${DAY} 100%`);
        return { background: `linear-gradient(90deg, ${stops.join(', ')})` };
    }

    if (ephem && ephem.midSunAltDeg >= -0.833) {
        return { background: DAY };
    }

    return {
        background: `linear-gradient(90deg, ${DAY} 0%, ${NAU} 22%, ${DRK} 50%, ${NAU} 78%, ${DAY} 100%)`
    };
});

const formattedObsTime = computed(() => formatLocalClock(effectiveTimeMs.value));
const formattedObsDate = computed(() =>
    formatObsDateBadge(effectiveTimeMs.value, liveNowMs.value)
);
const dateInputValue = computed(() => formatDateInputYmd(effectiveTimeMs.value));
const dayOffsetFromToday = computed(() => {
    const todayNoon = new Date(liveNowMs.value);
    todayNoon.setHours(12, 0, 0, 0);
    const obsNoon = new Date(effectiveTimeMs.value);
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

const isLoadingSky = ref(!preparedSky);
const cachedServerImages = ref([]);
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

// Coalesced pointer state
const isPanning = ref(false);
let panMoved = false;
let panStartScreen = { x: 0, y: 0 };
let panStartSky = { ra: 0, dec: 0 };
let pendingPointer = null; // { px, py, clientX, clientY }
let pinchStartDist = 0;
let pinchStartFov = 35.0;
let interactiveItems = [];

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
        // East tangent vector E
        Ex: -sinA0,
        Ey: cosA0,
        // North tangent vector N
        Nx: -sinD0 * cosA0,
        Ny: -sinD0 * sinA0,
        Nz: cosD0
    };
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
        x: cam.halfW - fk * (x * cam.Ex + y * cam.Ey),
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

const screenToSky = (px, py, centerRaDeg = viewRa.value, centerDecDeg = viewDec.value) => {
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
    readoutConstellation.value = findConstellationName(raDeg, decDeg);

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

// --- OBSERVATION TIME CONTROLS ---
const onTimeSliderInput = (e) => {
    const val = Number(e.target?.value);
    if (!Number.isFinite(val)) return;
    isLiveTime.value = false;
    scrubTimeMs.value = val;
    updateStatusReadout(lastReadoutCoords.ra, lastReadoutCoords.dec, true);
    scheduleDraw();
};

const stepObsTime = (deltaMinutes) => {
    const base = effectiveTimeMs.value;
    isLiveTime.value = false;
    scrubTimeMs.value = base + deltaMinutes * 60 * 1000;
    updateStatusReadout(lastReadoutCoords.ra, lastReadoutCoords.dec, true);
    scheduleDraw();
};

const stepObsDays = (deltaDays) => {
    const dt = new Date(effectiveTimeMs.value);
    dt.setDate(dt.getDate() + deltaDays);
    isLiveTime.value = false;
    scrubTimeMs.value = dt.getTime();
    updateStatusReadout(lastReadoutCoords.ra, lastReadoutCoords.dec, true);
    scheduleDraw();
};

const onDateInputChange = (e) => {
    const raw = e.target?.value;
    if (!raw || typeof raw !== 'string') return;
    const parts = raw.split('-').map(Number);
    if (parts.length !== 3 || parts.some((n) => !Number.isFinite(n))) return;
    const [year, month, day] = parts;

    const cur = new Date(effectiveTimeMs.value);
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

    isLiveTime.value = false;
    scrubTimeMs.value = next.getTime();
    updateStatusReadout(lastReadoutCoords.ra, lastReadoutCoords.dec, true);
    scheduleDraw();
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
    isLiveTime.value = false;
    scrubTimeMs.value = sessionRange.value.midMs;
    updateStatusReadout(lastReadoutCoords.ra, lastReadoutCoords.dec, true);
    scheduleDraw();
};

const resetToLiveNow = () => {
    liveNowMs.value = Date.now();
    isLiveTime.value = true;
    updateStatusReadout(lastReadoutCoords.ra, lastReadoutCoords.dec, true);
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
    const zE = cam.Ex * horiz.Zx + cam.Ey * horiz.Zy;
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
    const STEPS = 48;

    let minN = Infinity;
    let maxN = -Infinity;
    const ptsX = new Float64Array(STEPS + 1);
    const ptsY = new Float64Array(STEPS + 1);

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

const drawHorizonOverlay = (ctx, cam, w, h, fov, minPointCos, placedBoxes, overlapsPlaced) => {
    const loc = observerLocation.value;
    if (!loc) return;

    const horiz = makeHorizontalBasis(loc.lat, loc.lon, effectiveTimeMs.value);

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
            const msx = cam.halfW - mFk * (mx3 * cam.Ex + my3 * cam.Ey);
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
                    placedBoxes.push({ x1, y1, x2: x1 + pw, y2: y1 + ph });
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
        const sx = cam.halfW - fk * (px3 * cam.Ex + py3 * cam.Ey);
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

        placedBoxes.push({ x1, y1, x2: x1 + pw, y2: y1 + ph });

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
        const zsx = cam.halfW - zFk * (horiz.Zx * cam.Ex + horiz.Zy * cam.Ey);
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

// --- COMBINED CACHED SKY IMAGES & PRE-INDEXED DSO TABLE ---
const fetchCachedSkyImages = async () => {
    try {
        const res = await fetch('/api/cache/sky-images');
        if (!res.ok) return;
        const data = await res.json();
        if (Array.isArray(data?.images)) {
            cachedServerImages.value = data.images;
            scheduleDraw();
        }
    } catch (err) {
        console.warn('Could not load cached sky images:', err);
    }
};

const normalizePhotoKeyName = (name) =>
    String(name || '')
        .trim()
        .toUpperCase()
        .replace(/[\s_-]+/g, '');

const allSkyPhotos = computed(() => {
    const byKey = new Map();
    const atlasByNormName = new Map();
    const activeName = props.object?.name || '';

    // 1. Pre-baked Sky Atlas tiles (zero per-target HTTP / GPU texture overhead)
    const atlasList = skyAtlasItems.value;
    for (let i = 0; i < atlasList.length; i++) {
        const at = atlasList[i];
        const key = `${at.ra.toFixed(3)}_${at.dec.toFixed(3)}_${at.fov.toFixed(2)}`;
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
            isActiveTarget: Boolean(activeName && at.name === activeName),
            isCustom: Boolean(at.isCustom)
        };
        byKey.set(key, entry);
        const norm = normalizePhotoKeyName(at.name);
        if (norm && !atlasByNormName.has(norm)) {
            atlasByNormName.set(norm, entry);
        }
    }

    const mergeLivePhoto = (name, commonName, ra, dec, fov, url, isCustom) => {
        const norm = !isCustom ? normalizePhotoKeyName(name) : '';
        if (norm) {
            const existingAtlas = atlasByNormName.get(norm);
            if (
                existingAtlas &&
                Math.abs(existingAtlas.ra - ra) < 0.05 &&
                Math.abs(existingAtlas.dec - dec) < 0.05 &&
                Math.abs(existingAtlas.fov - fov) < 0.3
            ) {
                existingAtlas.url = url;
                if (activeName && name === activeName) {
                    existingAtlas.isActiveTarget = true;
                }
                return;
            }
        }

        const key = `${ra.toFixed(3)}_${dec.toFixed(3)}_${fov.toFixed(2)}`;
        const prev = byKey.get(key);
        if (prev) {
            prev.url = url;
            if (activeName && name === activeName) {
                prev.isActiveTarget = true;
            }
            return;
        }

        const a = ra * DEG2RAD;
        const d = dec * DEG2RAD;
        const cd = Math.cos(d);
        byKey.set(key, {
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
            isActiveTarget: Boolean(activeName && name === activeName),
            isCustom: Boolean(isCustom)
        });
    };

    // 2. Live cached high-res cutouts from /api/cache/sky-images
    for (const img of cachedServerImages.value) {
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

    // 3. Streamed session objects marked cached
    if (Array.isArray(props.objects)) {
        for (let i = 0; i < props.objects.length; i++) {
            const o = props.objects[i];
            if (!o || o.status !== 'cached' || !o.image_url) continue;
            const ra = Number(o.center_ra !== undefined ? o.center_ra : o.ra);
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

    // 4. Active selected target
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

    return Array.from(byKey.values()).sort((a, b) => {
        if (a.isActiveTarget !== b.isActiveTarget) return a.isActiveTarget ? 1 : -1;
        return b.fov - a.fov;
    });
});

// Pre-indexed non-reactive DSO list + O(1) object map (rebuilt only when catalog/photo state changes)
let preparedDsos = [];
let objectByNameMap = new Map();

const normCatalogKey = (s) => (s ? String(s).toLowerCase().replace(/\s+/g, '') : '');

const lookupCatalogObject = (name) => {
    if (!name) return null;
    return objectByNameMap.get(name) || objectByNameMap.get(normCatalogKey(name)) || null;
};

const rebuildDsoIndex = () => {
    const objs = props.objects;
    const byName = new Map();
    const cachedNames = new Set();
    const photos = allSkyPhotos.value;
    for (let i = 0; i < photos.length; i++) {
        if (photos[i].name) cachedNames.add(photos[i].name);
    }

    const list = [];
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

            const ra = parseFloat(obj.ra);
            const dec = parseFloat(obj.dec);
            if (!Number.isFinite(ra) || !Number.isFinite(dec)) continue;

            const a = ra * DEG2RAD;
            const d = dec * DEG2RAD;
            const cd = Math.cos(d);
            const isMessier = /^M\s*\d+$/i.test(obj.name);
            const shortName = isMessier ? obj.name.replace(/\s+/g, '') : obj.name;
            const mag = typeof obj.mag === 'number' && obj.mag < 90 ? obj.mag : 13.5;
            const maj = typeof obj.maj_ax === 'number' ? obj.maj_ax : 0;
            const isCached = obj.status === 'cached' || cachedNames.has(obj.name);

            list.push({
                obj,
                name: obj.name,
                shortName,
                commonName: obj.common_name && obj.common_name !== 'N/A' ? obj.common_name : '',
                ra,
                dec,
                x: cd * Math.cos(a),
                y: cd * Math.sin(a),
                z: Math.sin(d),
                isMessier,
                isCached,
                mag,
                maj,
                majHalfRad: (maj / 60.0) * DEG2RAD * 0.5,
                score: (isCached ? 200 : 0) + (isMessier ? 100 : 0) + Math.min(maj, 60) - mag * 3
            });
        }
    }

    list.sort((a, b) => b.score - a.score);
    preparedDsos = list;
    objectByNameMap = byName;
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
    const tgt = getTargetCoords();
    return angularDistanceDeg(viewRa.value, viewDec.value, tgt.ra, tgt.dec) > 0.35;
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
        const sx = cam.halfW - fk * (x * cam.Ex + y * cam.Ey);
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
    const centerRa = viewRa.value;
    const centerDec = viewDec.value;
    const cam = makeCameraBasis(centerRa, centerDec, w, h, fov);

    // Compute exact angular corner radius for tight cone culling
    const cornerRho = Math.hypot(w * 0.5, h * 0.5) / cam.f;
    const maxVisRad = Math.min(Math.PI * 0.68, 2.0 * Math.atan(cornerRho * 0.5) + 0.14);
    const minPointCos = Math.cos(maxVisRad);
    const cosVis = minPointCos;
    const sinVis = Math.sin(maxVisRad);
    const maxJumpPx = Math.max(w, h) * 1.4;
    const maxJumpSq = maxJumpPx * maxJumpPx;

    interactiveItems = [];
    const placedBoxes = [];

    const overlapsPlaced = (x1, y1, x2, y2, pad = 2) => {
        for (let i = 0; i < placedBoxes.length; i++) {
            const b = placedBoxes[i];
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
            const sx = cam.halfW - fk * (x * cam.Ex + y * cam.Ey);
            const sy = cam.halfH - fk * (x * cam.Nx + y * cam.Ny + z * cam.Nz);
            const rPx = mwNodes[i + 3] * fk;
            if (sx + rPx < 0 || sx - rPx > w || sy + rPx < 0 || sy - rPx > h) continue;

            ctx.globalAlpha = mwNodes[i + 4] * fovFade;
            ctx.drawImage(mwCloudSprite, sx - rPx, sy - rPx, rPx * 2.0, rPx * 2.0);
        }
        ctx.restore();
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

    // 5. Sky Atlas + Cached Sky Survey Photos Projected at True Celestial Scale
    // IMPORTANT: Each tile MUST draw its source-over dark backing rect immediately before its
    // screen-blended image so overlapping DSS2 cutouts attenuate underlying tiles instead of blowing out to white.
    const hovered = hoveredItem.value;
    if (showCachedPhotos.value && allSkyPhotos.value.length > 0) {
        const photos = allSkyPhotos.value;
        const denseCatalogMode = photos.length > 120;
        const minRenderPx = denseCatalogMode ? 3.2 : 2.5;
        const photoCullCos = Math.cos(Math.min(Math.PI * 0.72, maxVisRad + 0.15));
        let hoveredHighlight = null;

        ctx.fillStyle = denseCatalogMode ? 'rgba(4, 7, 14, 0.44)' : 'rgba(4, 7, 14, 0.62)';

        for (let i = 0; i < photos.length; i++) {
            const photo = photos[i];
            const cosC = photo.x * cam.Cx + photo.y * cam.Cy + photo.z * cam.Cz;
            if (cosC < photoCullCos) continue;

            const k = 2.0 / (1.0 + cosC);
            const fk = cam.f * k;
            const sizePx = photo.fov * DEG2RAD * fk;
            if (sizePx < minRenderPx && !photo.isActiveTarget) continue;

            const u = photo.x * cam.Ex + photo.y * cam.Ey;
            const v = photo.x * cam.Nx + photo.y * cam.Ny + photo.z * cam.Nz;
            const px = cam.halfW - fk * u;
            const py = cam.halfH - fk * v;

            if (
                px + sizePx < -40 ||
                px - sizePx > w + 40 ||
                py + sizePx < -40 ||
                py - sizePx > h + 40
            ) {
                continue;
            }

            if (!photo.isActiveTarget && sizePx >= (denseCatalogMode ? 4.5 : 3.0)) {
                interactiveItems.push({
                    kind: 'photo',
                    name: photo.name,
                    commonName: photo.commonName,
                    fov: photo.fov,
                    ra: photo.ra,
                    dec: photo.dec,
                    x: px,
                    y: py,
                    hitR: Math.max(sizePx * 0.5, 9),
                    refObj: lookupCatalogObject(photo.name),
                    isCustom: photo.isCustom
                });
            }

            const isHovered =
                hovered?.kind === 'photo' &&
                hovered?.name === photo.name &&
                Math.abs(hovered.ra - photo.ra) < 0.01;

            const tile = photo.atlasTile;
            const sheetImg = tile && sizePx >= 3.2 ? ensureAtlasSheetImage(tile.s) : null;
            const wantHiRes =
                Boolean(photo.url) &&
                (!tile || sizePx >= 190 || (photo.isActiveTarget && sizePx >= 115));
            const hiResImg =
                wantHiRes && sizePx >= 4.5
                    ? ensureImageLoaded(photo.url, !isPanning.value || photo.isActiveTarget)
                    : null;

            const hasReadyImg = Boolean((hiResImg || sheetImg) && sizePx >= 4.0);
            if (!hasReadyImg && !isHovered) {
                continue;
            }

            // Algebraic conformal local-north screen basis (zero sin/cos/atan2 calls)
            const vx = -photo.x * photo.z;
            const vy = -photo.y * photo.z;
            const vz = 1.0 - photo.z * photo.z;
            let cosA = 1.0;
            let sinA = 0.0;
            if (vz > 1e-8) {
                const vDotC = vx * cam.Cx + vy * cam.Cy + vz * cam.Cz;
                const vDotE = vx * cam.Ex + vy * cam.Ey;
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
                ctx.globalAlpha = photo.isActiveTarget ? 0.96 : 0.88;
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
                placedBoxes.push({
                    x1: tp.x - tw * 0.5 - 6,
                    y1: by - 14,
                    x2: tp.x + tw * 0.5 + 6,
                    y2: tp.y + ringR + 6
                });
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
                const sx = cam.halfW - fk * (x * cam.Ex + y * cam.Ey);
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
            const sx = cam.halfW - fk * (x * cam.Ex + y * cam.Ey);
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
            const sx = cam.halfW - fk * (x * cam.Ex + y * cam.Ey);
            const sy = cam.halfH - fk * (x * cam.Nx + y * cam.Ny + z * cam.Nz);
            if (sx <= 30 || sx >= w - 65 || sy <= 24 || sy >= h - 30) continue;

            const tw = getMeasuredTextWidth(ctx, 'star', starName);
            const lx = sx + starRadius[ls.idx] + 5;
            const x1 = lx - 2;
            const y1 = sy - 7;
            const x2 = lx + tw + 2;
            const y2 = sy + 7;

            if (!overlapsPlaced(x1, y1, x2, y2, 2)) {
                placedBoxes.push({ x1, y1, x2, y2 });
                ctx.strokeText(starName, lx, sy);
                ctx.fillStyle = ls.isBright ? '#fbbf24' : 'rgba(234, 179, 8, 0.92)';
                ctx.fillText(starName, lx, sy);
            }
        }
    }

    // 7. Nearby Deep-Sky Objects (Pre-sorted, pre-indexed, batched ring strokes)
    if (showDsos.value && preparedDsos.length > 0) {
        const activeName = props.object?.name || '';
        const maxMarkers = fov > 35 ? 28 : fov > 15 ? 42 : 65;
        const visibleDsos = [];

        for (let i = 0; i < preparedDsos.length; i++) {
            const d = preparedDsos[i];
            if (d.name === activeName) continue;
            if (fov > 35 && !d.isMessier && !d.isCached && d.mag > 7.2 && d.maj < 30) continue;
            if (fov > 16 && !d.isMessier && !d.isCached && d.mag > 9.0 && d.maj < 15) continue;

            const cosC = d.x * cam.Cx + d.y * cam.Cy + d.z * cam.Cz;
            if (cosC < minPointCos) continue;

            const fk = (cam.f * 2.0) / (1.0 + cosC);
            const sx = cam.halfW - fk * (d.x * cam.Ex + d.y * cam.Ey);
            if (sx < 10 || sx > w - 10) continue;
            const sy = cam.halfH - fk * (d.x * cam.Nx + d.y * cam.Ny + d.z * cam.Nz);
            if (sy < 14 || sy > h - 18) continue;

            const rPx = Math.max(3.4, Math.min(22, d.majHalfRad * fk));
            visibleDsos.push({ d, sx, sy, rPx });
            if (visibleDsos.length >= maxMarkers) break;
        }

        if (visibleDsos.length > 0) {
            // Batch standard DSO rings
            ctx.lineWidth = 1.25;
            ctx.strokeStyle = 'rgba(192, 132, 252, 0.85)';
            ctx.beginPath();
            for (let i = 0; i < visibleDsos.length; i++) {
                const v = visibleDsos[i];
                if (v.d.isCached || (hovered?.name === v.d.name && hovered?.kind !== 'active')) {
                    continue;
                }
                ctx.moveTo(v.sx + v.rPx, v.sy);
                ctx.arc(v.sx, v.sy, v.rPx, 0, TAU);
            }
            ctx.stroke();

            // Batch cached DSO rings
            ctx.strokeStyle = 'rgba(56, 189, 248, 0.9)';
            ctx.beginPath();
            for (let i = 0; i < visibleDsos.length; i++) {
                const v = visibleDsos[i];
                if (!v.d.isCached || (hovered?.name === v.d.name && hovered?.kind !== 'active')) {
                    continue;
                }
                ctx.moveTo(v.sx + v.rPx, v.sy);
                ctx.arc(v.sx, v.sy, v.rPx, 0, TAU);
            }
            ctx.stroke();

            // Labels & hovered highlight (saturated cyan/violet + dark outline halo)
            ctx.font = '600 11px system-ui, -apple-system, sans-serif';
            ctx.textAlign = 'left';
            ctx.textBaseline = 'middle';
            ctx.lineJoin = 'round';
            ctx.miterLimit = 2;

            for (let i = 0; i < visibleDsos.length; i++) {
                const { d, sx, sy, rPx } = visibleDsos[i];
                if (hovered?.name === d.name && hovered?.kind !== 'active') {
                    ctx.strokeStyle = '#38bdf8';
                    ctx.lineWidth = 1.8;
                    ctx.beginPath();
                    ctx.arc(sx, sy, rPx, 0, TAU);
                    ctx.stroke();
                }

                const lx = sx + rPx + 4.5;
                const tw = getMeasuredTextWidth(ctx, 'dso', d.shortName);
                const x1 = lx - 2;
                const y1 = sy - 6.5;
                const x2 = lx + tw + 2;
                const y2 = sy + 6.5;

                if (!overlapsPlaced(x1, y1, x2, y2, 2)) {
                    placedBoxes.push({ x1, y1, x2, y2 });
                    ctx.lineWidth = 3.2;
                    ctx.strokeStyle = 'rgba(3, 6, 12, 0.94)';
                    ctx.strokeText(d.shortName, lx, sy);
                    ctx.fillStyle = d.isCached ? '#38bdf8' : '#c084fc';
                    ctx.fillText(d.shortName, lx, sy);
                }

                interactiveItems.push({
                    kind: 'dso',
                    name: d.name,
                    commonName: d.commonName,
                    fov: d.obj.image_fov || null,
                    ra: d.ra,
                    dec: d.dec,
                    x: sx,
                    y: sy,
                    hitR: Math.max(rPx + 5, 10),
                    refObj: d.obj
                });
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
            const sx = cam.halfW - fk * (c.x * cam.Ex + c.y * cam.Ey);
            const sy = cam.halfH - fk * (c.x * cam.Nx + c.y * cam.Ny + c.z * cam.Nz);
            if (sx < 55 || sx > w - 55 || sy < 36 || sy > h - 36) continue;

            const tw = getMeasuredTextWidth(ctx, 'const', c.tracked);
            const x1 = sx - tw * 0.5 - 4;
            const y1 = sy - 8;
            const x2 = sx + tw * 0.5 + 4;
            const y2 = sy + 8;

            if (!overlapsPlaced(x1, y1, x2, y2, 3)) {
                placedBoxes.push({ x1, y1, x2, y2 });
                ctx.strokeText(c.tracked, sx, sy);
                ctx.fillText(c.tracked, sx, sy);
            }
        }
    }

    // 8.5 Local Observer Horizon, Ground Veil, Min-Altitude Ring & Compass Points
    if (showHorizon.value && observerLocation.value) {
        drawHorizonOverlay(ctx, cam, w, h, fov, minPointCos, placedBoxes, overlapsPlaced);
    }

    // 9. Active Target Highlight Ring, Rotated Sensor FOV Frame & Comparison Setups
    if (props.object) {
        const sensorCoords = getTargetCoords();
        const sp = projectRaDecWithBasis(sensorCoords.ra, sensorCoords.dec, cam);

        if (sp && Array.isArray(props.comparisonRectangles) && props.comparisonRectangles.length > 0) {
            const localNorth = computeLocalNorthAngleWithBasis(
                sensorCoords.ra,
                sensorCoords.dec,
                sp,
                cam
            );
            const totalRotRad = localNorth + (Number(props.rotation) || 0) * DEG2RAD;

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
            const localNorth = computeLocalNorthAngleWithBasis(
                sensorCoords.ra,
                sensorCoords.dec,
                sp,
                cam
            );
            const totalRotRad = localNorth + (Number(props.rotation) || 0) * DEG2RAD;
            const sw = props.sensorFov.w * DEG2RAD * sp.f * sp.k;
            const sh = props.sensorFov.h * DEG2RAD * sp.f * sp.k;

            ctx.save();
            ctx.translate(sp.x, sp.y);
            ctx.rotate(totalRotRad);

            const drawW = Math.max(sw, 6);
            const drawH = Math.max(sh, 4.5);
            // Zero-cost double-stroke glow (replaces expensive Gaussian ctx.shadowBlur)
            ctx.strokeStyle = 'rgba(245, 158, 11, 0.28)';
            ctx.lineWidth = 4.6;
            ctx.strokeRect(-drawW * 0.5, -drawH * 0.5, drawW, drawH);

            ctx.strokeStyle = '#f59e0b';
            ctx.lineWidth = 1.85;
            ctx.strokeRect(-drawW * 0.5, -drawH * 0.5, drawW, drawH);
            ctx.restore();
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

            interactiveItems.push({
                kind: 'active',
                name: props.object.name,
                commonName:
                    props.object.common_name && props.object.common_name !== 'N/A'
                        ? props.object.common_name
                        : '',
                fov: props.object.image_fov || props.baseImageFov,
                ra: tp.ra,
                dec: tp.dec,
                x: tp.x,
                y: tp.y,
                hitR: ringR
            });
        }
    }
};

// --- INTERACTION HANDLERS (rAF-Coalesced Pan, Wheel Zoom, Touch Pinch, Click/Hover) ---
const centerOnTarget = () => {
    const tgt = getTargetCoords();
    if (animFrameId) cancelAnimationFrame(animFrameId);

    const startRa = viewRa.value;
    const startDec = viewDec.value;
    let dRa = tgt.ra - startRa;
    if (dRa > 180) dRa -= 360;
    if (dRa < -180) dRa += 360;
    const dDec = tgt.dec - startDec;

    if (Math.hypot(dRa, dDec) < 0.05) {
        viewRa.value = tgt.ra;
        viewDec.value = tgt.dec;
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
            updateStatusReadout(viewRa.value, viewDec.value, true);
        }
    };
    animFrameId = requestAnimationFrame(step);
};

const applyZoomFactor = (factor, anchorPx = null, anchorPy = null) => {
    const prevFov = mapFovDeg.value;
    const minMapFov = Math.min(0.35, (props.baseImageFov || 1.0) * 0.8);
    const nextFov = Math.max(minMapFov, Math.min(105.0, prevFov * factor));

    if (factor < 1.0 && props.object) {
        const tgt = getTargetCoords();
        const distToTarget = angularDistanceDeg(viewRa.value, viewDec.value, tgt.ra, tgt.dec);
        const returnThreshold = Math.max((props.baseImageFov || 1.0) * 1.03, 0.28);
        if (
            distToTarget <= Math.max((props.baseImageFov || 1.0) * 1.15, 0.9) &&
            nextFov <= returnThreshold
        ) {
            emit('close');
            return;
        }
    }

    if (anchorPx !== null && anchorPy !== null) {
        const beforeSky = screenToSky(anchorPx, anchorPy);
        mapFovDeg.value = nextFov;
        const afterSky = screenToSky(anchorPx, anchorPy);
        let dRa = beforeSky.ra - afterSky.ra;
        if (dRa > 180) dRa -= 360;
        if (dRa < -180) dRa += 360;
        viewRa.value = wrapRaDeg(viewRa.value + dRa);
        viewDec.value = clampDecDeg(viewDec.value + (beforeSky.dec - afterSky.dec));
    } else {
        mapFovDeg.value = nextFov;
        updateStatusReadout(viewRa.value, viewDec.value);
    }

    scheduleDraw();
};

const onWheel = (e) => {
    const rect = canvasRef.value?.getBoundingClientRect();
    const px = rect ? e.clientX - rect.left : null;
    const py = rect ? e.clientY - rect.top : null;
    const factor = e.deltaY > 0 ? 1.14 : 1 / 1.14;
    applyZoomFactor(factor, px, py);
};

const findHitItem = (px, py) => {
    let best = null;
    let bestDistSq = Infinity;
    for (let i = 0; i < interactiveItems.length; i++) {
        const item = interactiveItems[i];
        const dx = px - item.x;
        const dy = py - item.y;
        const dSq = dx * dx + dy * dy;
        if (dSq <= item.hitR * item.hitR && dSq < bestDistSq) {
            best = item;
            bestDistSq = dSq;
        }
    }
    return best;
};

const flushPointerFrame = () => {
    pointerRafId = null;
    const ptr = pendingPointer;
    if (!ptr) return;

    if (isPanning.value) {
        const dx = ptr.clientX - panStartScreen.x;
        const dy = ptr.clientY - panStartScreen.y;
        if (dx * dx + dy * dy > 9) panMoved = true;

        const shiftedSky = screenToSky(
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

    const hit = findHitItem(ptr.px, ptr.py);
    if (hit?.name !== hoveredItem.value?.name || hit?.kind !== hoveredItem.value?.kind) {
        hoveredItem.value = hit;
        drawSkyCanvas();
    } else if (hit) {
        hoveredItem.value = hit;
    }
};

const onMouseDown = (e) => {
    if (e.button !== 0) return;
    if (animFrameId) {
        cancelAnimationFrame(animFrameId);
        animFrameId = null;
    }
    isPanning.value = true;
    panMoved = false;
    panStartScreen = { x: e.clientX, y: e.clientY };
    panStartSky = { ra: viewRa.value, dec: viewDec.value };
};

const onMouseMove = (e) => {
    const rect = canvasRef.value?.getBoundingClientRect();
    if (!rect) return;
    pendingPointer = {
        px: e.clientX - rect.left,
        py: e.clientY - rect.top,
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
    updateStatusReadout(viewRa.value, viewDec.value, true);
    scheduleDraw();
};

const onDoubleClick = (e) => {
    const rect = canvasRef.value?.getBoundingClientRect();
    if (!rect) return;
    const hit = findHitItem(e.clientX - rect.left, e.clientY - rect.top);
    if (hit) {
        handleItemClick(hit, true);
    }
};

const onMouseUp = (e) => {
    const wasPanning = isPanning.value;
    isPanning.value = false;
    if (wasPanning && !panMoved) {
        const rect = canvasRef.value?.getBoundingClientRect();
        if (rect) {
            const hit = findHitItem(e.clientX - rect.left, e.clientY - rect.top);
            if (hit) {
                handleItemClick(hit);
            }
        }
    } else if (wasPanning) {
        updateStatusReadout(viewRa.value, viewDec.value, true);
    }
};

const onMouseLeave = () => {
    isPanning.value = false;
    pendingPointer = null;
    updateStatusReadout(viewRa.value, viewDec.value, true);
    if (hoveredItem.value) {
        hoveredItem.value = null;
        scheduleDraw();
    }
};

const getTouchDist = (t1, t2) => Math.hypot(t2.clientX - t1.clientX, t2.clientY - t1.clientY);

const onTouchStart = (e) => {
    if (e.touches.length === 2) {
        isPanning.value = false;
        pinchStartDist = getTouchDist(e.touches[0], e.touches[1]);
        pinchStartFov = mapFovDeg.value;
    } else if (e.touches.length === 1) {
        const t = e.touches[0];
        isPanning.value = true;
        panMoved = false;
        panStartScreen = { x: t.clientX, y: t.clientY };
        panStartSky = { ra: viewRa.value, dec: viewDec.value };
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
            px: 0,
            py: 0,
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
        isPanning.value = false;
        pinchStartDist = 0;
        updateStatusReadout(viewRa.value, viewDec.value, true);
    }
};

const onKeyDown = (e) => {
    if (e.key === 'Escape') {
        emit('close');
    }
};

// --- LIGHTWEIGHT WATCHERS & LIFECYCLE ---
watch(
    () => [props.objects?.length, allSkyPhotos.value.length],
    () => {
        rebuildDsoIndex();
        scheduleDraw();
    },
    { immediate: true }
);

watch(
    () => props.object?.name,
    (newName, oldName) => {
        if (newName && newName !== oldName) {
            const tgt = getTargetCoords();
            viewRa.value = tgt.ra;
            viewDec.value = tgt.dec;
            updateStatusReadout(tgt.ra, tgt.dec, true);
            scheduleDraw();
        }
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
        showHorizon.value,
        observerLocation.value?.lat,
        observerLocation.value?.lon,
        minAltitudeDeg.value
    ],
    ([, newStatus]) => {
        if (newStatus === 'cached') {
            fetchCachedSkyImages();
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

watch(skyAtlasVersion, () => {
    scheduleDraw();
});

onMounted(async () => {
    window.addEventListener('keydown', onKeyDown);

    liveClockTimer = window.setInterval(() => {
        if (isLiveTime.value) {
            liveNowMs.value = Date.now();
            updateStatusReadout(lastReadoutCoords.ra, lastReadoutCoords.dec, true);
            if (showHorizon.value && observerLocation.value) {
                scheduleDraw();
            }
        }
    }, 30000);

    if (containerRef.value) {
        const updateDims = () => {
            if (!containerRef.value) return;
            viewportW.value = containerRef.value.clientWidth || 800;
            viewportH.value = containerRef.value.clientHeight || 600;
            scheduleDraw();
        };
        resizeObserver = new ResizeObserver(updateDims);
        resizeObserver.observe(containerRef.value);
        updateDims();
    }

    ensureSkyAtlasLoaded();
    fetchCachedSkyImages();
    await loadSkyData();
    isLoadingSky.value = false;
    updateStatusReadout(viewRa.value, viewDec.value, true);
    scheduleDraw();
});

onUnmounted(() => {
    window.removeEventListener('keydown', onKeyDown);
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
        :class="{ 'is-panning': isPanning, 'has-hover-hit': Boolean(hoveredItem) }"
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

        <!-- Top-Left Navigation Controls -->
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
        </div>

        <!-- Top-Right Layer Toggles & Quick Zoom -->
        <div class="sky-top-right-bar" @mousedown.stop @touchstart.stop>
            <button
                v-if="observerLocation"
                type="button"
                class="sky-layer-chip horizon-chip"
                :class="{ active: showHorizon }"
                @click="showHorizon = !showHorizon"
                :title="`Toggle local horizon, cardinal directions & min altitude (${observerLocation.coordsLabel})`"
            >
                <span>⛰ Horizon</span>
            </button>
            <button
                type="button"
                class="sky-layer-chip"
                :class="{ active: showCachedPhotos }"
                @click="showCachedPhotos = !showCachedPhotos"
                title="Project already-downloaded sky survey images at their true celestial coordinates and FOV"
            >
                <span>📷 Photos ({{ allSkyPhotos.length }})</span>
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

        <!-- Hover Tooltip for Downloaded Photos & DSOs -->
        <div
            v-if="hoveredItem && !isPanning"
            class="sky-hover-tooltip"
            :style="{
                left: `${Math.min(Math.max(hoveredItem.x + 14, 12), viewportW - 220)}px`,
                top: `${Math.max(hoveredItem.y - 36, 48)}px`
            }"
        >
            <div class="tooltip-title">
                <span v-if="hoveredItem.kind === 'photo'" class="tooltip-badge">📷 Cached</span>
                <strong>{{ hoveredItem.name }}</strong>
                <span v-if="hoveredItem.commonName" class="tooltip-common">
                    · {{ hoveredItem.commonName }}
                </span>
            </div>
            <div class="tooltip-sub">
                <span v-if="hoveredItem.kind === 'active'">Click to return to close-up Framing</span>
                <span v-else-if="hoveredItem.fov">
                    Downloaded {{ Number(hoveredItem.fov).toFixed(2) }}° FOV · Click to select
                </span>
                <span v-else>Click to select target</span>
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
        <div
            v-if="showHorizon && observerLocation"
            class="sky-time-bar"
            @mousedown.stop
            @touchstart.stop
            @wheel.stop
        >
            <div class="time-date-row">
                <div class="date-step-group">
                    <button
                        type="button"
                        class="time-mini-btn"
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
                        class="time-mini-btn"
                        @click="stepObsDays(7)"
                        title="Step forward 7 days (keep clock time)"
                    >
                        +7d
                    </button>
                </div>
            </div>

            <div class="time-bar-top">
                <span class="loc-pill" :title="observerLocation.coordsLabel">
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
        </div>
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

.sky-context-root.is-panning {
    cursor: grabbing;
}

.sky-context-root.has-hover-hit:not(.is-panning) {
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
    gap: 8px;
    z-index: 32;
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

/* Hover Tooltip */
.sky-hover-tooltip {
    position: absolute;
    pointer-events: none;
    background: rgba(10, 14, 24, 0.95);
    border: 1px solid rgba(56, 189, 248, 0.45);
    border-radius: 7px;
    padding: 5px 10px;
    z-index: 35;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.55);
    max-width: 260px;
}

.tooltip-title {
    display: flex;
    align-items: center;
    gap: 5px;
    font-size: 0.78rem;
    color: #f8fafc;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.tooltip-badge {
    font-size: 0.68rem;
    padding: 1px 5px;
    border-radius: 4px;
    background: rgba(56, 189, 248, 0.18);
    color: #7dd3fc;
}

.tooltip-common {
    color: #cbd5e1;
    font-weight: 400;
}

.tooltip-sub {
    font-size: 0.69rem;
    color: #94a3b8;
    margin-top: 2px;
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
    display: flex;
    flex-direction: column;
    gap: 5px;
    padding: 6px 10px;
    border-radius: 8px;
    background: rgba(9, 14, 23, 0.94);
    border: 1px solid rgba(148, 163, 184, 0.26);
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.5);
    z-index: 33;
    min-width: 288px;
    max-width: 356px;
}

.time-date-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 5px;
    padding-bottom: 4px;
    border-bottom: 1px solid rgba(148, 163, 184, 0.14);
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
    .sky-time-bar {
        bottom: 42px;
        right: 12px;
        left: 12px;
        max-width: none;
    }
}

@media (max-width: 768px) {
    .sky-top-right-bar {
        top: 52px;
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
