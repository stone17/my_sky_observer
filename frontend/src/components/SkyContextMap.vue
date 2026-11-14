<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue';

const props = defineProps({
    object: { type: Object, default: null },
    objects: { type: Array, default: () => [] },
    settings: { type: Object, default: () => ({}) },
    clientSettings: { type: Object, default: () => ({}) },
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

const isLoadingSky = ref(!preparedSky);
const cachedServerImages = ref([]);
const hoveredItem = ref(null);

// Throttled status bar readout refs (decoupled from high-frequency mousemove events)
const readoutRaText = ref(formatRaHms(initialCoords.ra));
const readoutDecText = ref(formatDecDms(initialCoords.dec));
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
let resizeObserver = null;
let lastReadoutTs = 0;

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
    readoutRaText.value = formatRaHms(raDeg);
    readoutDecText.value = formatDecDms(decDeg);
    readoutConstellation.value = findConstellationName(raDeg, decDeg);
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

const allSkyPhotos = computed(() => {
    const byKey = new Map();

    for (const img of cachedServerImages.value) {
        if (!img?.url || img.ra === undefined || img.dec === undefined) continue;
        const cleanUrl = String(img.url).split('?')[0];
        const ra = Number(img.ra);
        const dec = Number(img.dec);
        const fov = Math.max(Number(img.fov) || 1.0, 0.2);
        const a = ra * DEG2RAD;
        const d = dec * DEG2RAD;
        const cd = Math.cos(d);
        const key = `${ra.toFixed(3)}_${dec.toFixed(3)}_${fov.toFixed(2)}`;
        byKey.set(key, {
            name: img.name,
            commonName: img.common_name || '',
            ra,
            dec,
            x: cd * Math.cos(a),
            y: cd * Math.sin(a),
            z: Math.sin(d),
            fov,
            url: cleanUrl,
            isActiveTarget: Boolean(props.object && img.name === props.object.name),
            isCustom: Boolean(img.is_custom)
        });
    }

    if (Array.isArray(props.objects)) {
        for (let i = 0; i < props.objects.length; i++) {
            const o = props.objects[i];
            if (!o || o.status !== 'cached' || !o.image_url) continue;
            const ra = Number(o.center_ra !== undefined ? o.center_ra : o.ra);
            const dec = Number(o.center_dec !== undefined ? o.center_dec : o.dec);
            const fov = Math.max(Number(o.image_fov || props.baseImageFov || 1.0), 0.2);
            if (!Number.isFinite(ra) || !Number.isFinite(dec)) continue;
            const a = ra * DEG2RAD;
            const d = dec * DEG2RAD;
            const cd = Math.cos(d);
            const key = `${ra.toFixed(3)}_${dec.toFixed(3)}_${fov.toFixed(2)}`;
            byKey.set(key, {
                name: o.name,
                commonName: o.common_name && o.common_name !== 'N/A' ? o.common_name : '',
                ra,
                dec,
                x: cd * Math.cos(a),
                y: cd * Math.sin(a),
                z: Math.sin(d),
                fov,
                url: String(o.image_url),
                isActiveTarget: Boolean(props.object && o.name === props.object.name),
                isCustom: Boolean(o.custom_framed)
            });
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
            const a = ra * DEG2RAD;
            const d = dec * DEG2RAD;
            const cd = Math.cos(d);
            const key = `${ra.toFixed(3)}_${dec.toFixed(3)}_${fov.toFixed(2)}`;
            byKey.set(key, {
                name: props.object.name,
                commonName:
                    props.object.common_name && props.object.common_name !== 'N/A'
                        ? props.object.common_name
                        : '',
                ra,
                dec,
                x: cd * Math.cos(a),
                y: cd * Math.sin(a),
                z: Math.sin(d),
                fov,
                url: String(props.object.image_url),
                isActiveTarget: true,
                isCustom: Boolean(props.object.custom_framed)
            });
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

const ensureImageLoaded = (url) => {
    if (!url) return null;
    const existing = imageElementCache.get(url);
    if (existing) {
        return existing.status === 'ready' ? existing.img : null;
    }
    const img = new Image();
    const record = { img, status: 'loading' };
    imageElementCache.set(url, record);
    img.onload = () => {
        record.status = 'ready';
        scheduleDraw();
    };
    img.onerror = () => {
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

    // 5. Already-Downloaded Sky Survey Photos Projected at True Celestial Scale
    const hovered = hoveredItem.value;
    if (showCachedPhotos.value && allSkyPhotos.value.length > 0) {
        const photos = allSkyPhotos.value;
        const photoCullCos = Math.cos(Math.min(Math.PI * 0.72, maxVisRad + 0.15));

        for (let i = 0; i < photos.length; i++) {
            const photo = photos[i];
            const cosC = photo.x * cam.Cx + photo.y * cam.Cy + photo.z * cam.Cz;
            if (cosC < photoCullCos) continue;

            const k = 2.0 / (1.0 + cosC);
            const fk = cam.f * k;
            const px = cam.halfW - fk * (photo.x * cam.Ex + photo.y * cam.Ey);
            const py = cam.halfH - fk * (photo.x * cam.Nx + photo.y * cam.Ny + photo.z * cam.Nz);
            const sizePx = photo.fov * DEG2RAD * fk;

            if (
                px + sizePx < -40 ||
                px - sizePx > w + 40 ||
                py + sizePx < -40 ||
                py - sizePx > h + 40
            ) {
                continue;
            }

            const projPt = { x: px, y: py, k, cosC, f: cam.f };
            const northAngle = computeLocalNorthAngleWithBasis(photo.ra, photo.dec, projPt, cam);
            const isHovered =
                hovered?.kind === 'photo' &&
                hovered?.name === photo.name &&
                Math.abs(hovered.ra - photo.ra) < 0.01;

            const imgEl = sizePx >= 3.5 ? ensureImageLoaded(photo.url) : null;

            ctx.save();
            ctx.translate(px, py);
            ctx.rotate(northAngle);

            if (imgEl && sizePx >= 5) {
                const half = sizePx * 0.5;
                ctx.fillStyle = 'rgba(4, 7, 14, 0.62)';
                ctx.fillRect(-half, -half, sizePx, sizePx);

                ctx.save();
                ctx.globalAlpha = photo.isActiveTarget ? 0.96 : 0.88;
                ctx.globalCompositeOperation = 'screen';
                ctx.drawImage(imgEl, -half, -half, sizePx, sizePx);
                ctx.restore();

                ctx.strokeStyle = photo.isActiveTarget
                    ? 'rgba(251, 191, 36, 0.58)'
                    : isHovered
                      ? 'rgba(56, 189, 248, 0.9)'
                      : 'rgba(56, 189, 248, 0.42)';
                ctx.lineWidth = isHovered ? 1.6 : 1.0;
                ctx.strokeRect(-half, -half, sizePx, sizePx);
            } else if (!photo.isActiveTarget) {
                const boxSz = Math.max(sizePx, 8);
                const half = boxSz * 0.5;
                ctx.fillStyle = isHovered
                    ? 'rgba(56, 189, 248, 0.28)'
                    : 'rgba(56, 189, 248, 0.14)';
                ctx.strokeStyle = isHovered
                    ? 'rgba(125, 211, 252, 0.95)'
                    : 'rgba(56, 189, 248, 0.68)';
                ctx.lineWidth = 1.2;
                ctx.fillRect(-half, -half, boxSz, boxSz);
                ctx.strokeRect(-half, -half, boxSz, boxSz);
            }
            ctx.restore();

            if (!photo.isActiveTarget) {
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
                    refObj: objectByNameMap.get(photo.name) || null,
                    isCustom: photo.isCustom
                });
            }
        }
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

        // 6c. Star proper names (only iterates over ~320 named stars)
        ctx.font = '500 11.5px system-ui, -apple-system, sans-serif';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'middle';

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
                ctx.fillStyle = 'rgba(3, 6, 12, 0.75)';
                ctx.fillText(starName, lx + 1, sy + 1);
                ctx.fillStyle = ls.isBright
                    ? 'rgba(232, 240, 252, 0.92)'
                    : 'rgba(196, 210, 232, 0.78)';
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
            ctx.lineWidth = 1.2;
            ctx.strokeStyle = 'rgba(167, 139, 250, 0.78)';
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
            ctx.strokeStyle = 'rgba(56, 189, 248, 0.85)';
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

            // Labels & hovered highlight
            ctx.font = '500 11px system-ui, -apple-system, sans-serif';
            ctx.textAlign = 'left';
            ctx.textBaseline = 'middle';

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
                    ctx.fillStyle = 'rgba(3, 6, 12, 0.78)';
                    ctx.fillText(d.shortName, lx + 1, sy + 1);
                    ctx.fillStyle = d.isCached
                        ? 'rgba(125, 211, 252, 0.94)'
                        : 'rgba(186, 164, 252, 0.88)';
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

    // 8. Constellation Names (ORION, MONOCEROS, LEPUS)
    if (showConstellations.value && fov >= 7 && preparedSky) {
        ctx.font = '600 11px system-ui, -apple-system, sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';

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
                ctx.fillStyle = 'rgba(3, 6, 12, 0.65)';
                ctx.fillText(c.tracked, sx + 1, sy + 1);
                ctx.fillStyle = 'rgba(138, 168, 210, 0.58)';
                ctx.fillText(c.tracked, sx, sy);
            }
        }
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

const handleItemClick = async (item) => {
    if (!item) return;
    if (item.kind === 'active') {
        emit('close');
        return;
    }
    if (item.refObj) {
        emit('select-object', item.refObj);
        return;
    }
    if (!item.isCustom && item.name) {
        try {
            const res = await fetch(`/api/object-details/${encodeURIComponent(item.name)}`);
            if (res.ok) {
                const details = await res.json();
                if (details?.name) {
                    emit('select-object', details);
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
        showDsos.value
    ],
    ([, newStatus]) => {
        if (newStatus === 'cached') {
            fetchCachedSkyImages();
        }
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

onMounted(async () => {
    window.addEventListener('keydown', onKeyDown);

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

    fetchCachedSkyImages();
    await loadSkyData();
    isLoadingSky.value = false;
    updateStatusReadout(viewRa.value, viewDec.value, true);
    scheduleDraw();
});

onUnmounted(() => {
    window.removeEventListener('keydown', onKeyDown);
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

        <!-- Bottom-Left Live Coordinate / Constellation / FOV Readout -->
        <div class="sky-bottom-left-bar">
            <span class="coord-seg">RA {{ readoutRaText }}</span>
            <span class="coord-seg">Dec {{ readoutDecText }}</span>
            <span v-if="readoutConstellation" class="coord-seg const-seg">
                {{ readoutConstellation }}
            </span>
            <span class="coord-seg fov-seg">FOV {{ readoutFovText }}</span>
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

/* Bottom-Left Status Readout (RA / Dec / Constellation / FOV) */
.sky-bottom-left-bar {
    position: absolute;
    bottom: 10px;
    left: 12px;
    display: inline-flex;
    align-items: center;
    gap: 14px;
    padding: 4px 10px;
    border-radius: 6px;
    background: rgba(6, 9, 16, 0.86);
    color: #cbd5e1;
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 0.76rem;
    letter-spacing: 0.01em;
    pointer-events: none;
    z-index: 32;
}

.const-seg {
    font-family: system-ui, -apple-system, sans-serif;
    color: #e2e8f0;
    font-weight: 500;
}

.fov-seg {
    color: #93c5fd;
}

@media (max-width: 768px) {
    .sky-top-right-bar {
        top: 52px;
        left: 12px;
        right: auto;
        justify-content: flex-start;
    }

    .sky-bottom-left-bar {
        gap: 9px;
        font-size: 0.69rem;
        right: 12px;
        flex-wrap: wrap;
    }
}
</style>
