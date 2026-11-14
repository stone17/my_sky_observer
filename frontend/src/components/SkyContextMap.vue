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

// --- MODULE-LEVEL CACHES ---
let skyDataCache = null;
let skyDataPromise = null;
const imageElementCache = new Map(); // url -> { img, status: 'loading' | 'ready' | 'error' }

const loadSkyData = async () => {
    if (skyDataCache) return skyDataCache;
    if (!skyDataPromise) {
        skyDataPromise = fetch('/sky_data.json')
            .then((r) => {
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                return r.json();
            })
            .then((data) => {
                skyDataCache = data;
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

// --- MATH & COORDINATE HELPERS ---
const DEG2RAD = Math.PI / 180.0;
const RAD2DEG = 180.0 / Math.PI;

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

const bvToRgb = (bv) => {
    const v = Number.isFinite(bv) ? bv : 0.4;
    if (v < -0.1) return [182, 212, 255]; // Blue-white O/B (Rigel, Alnitak)
    if (v < 0.25) return [220, 234, 255]; // Crisp white-blue A (Sirius, Vega)
    if (v < 0.6) return [255, 249, 238];  // Warm white F
    if (v < 1.05) return [255, 230, 184]; // Golden yellow G/K (Capella, Pollux)
    return [255, 192, 136];               // Orange-red K/M (Betelgeuse, Aldebaran)
};

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

const isLoadingSky = ref(!skyDataCache);
const cachedServerImages = ref([]);
const hoveredItem = ref(null); // { kind: 'dso' | 'photo', name, commonName, fov, ra, dec, x, y, refObj }
const cursorSky = ref(null);   // { ra, dec } when pointer moves over canvas

let rafId = null;
let animFrameId = null;
let resizeObserver = null;

// Interactive drag / pinch state
const isPanning = ref(false);
let panMoved = false;
let panStartScreen = { x: 0, y: 0 };
let panStartSky = { ra: 0, dec: 0 };
let pinchStartDist = 0;
let pinchStartFov = 35.0;
let interactiveItems = []; // Rebuilt per draw for fast click/hover hit-testing

// --- PROJECTION ENGINE (Stereographic, Astronomical North-Up, East-Left) ---
const getFocalLengthPx = () => {
    const h = Math.max(viewportH.value, 120);
    const fovRad = Math.max(0.25, Math.min(125.0, mapFovDeg.value)) * DEG2RAD;
    return (h * 0.5) / Math.tan(fovRad * 0.5);
};

const projectSkyToScreen = (raDeg, decDeg, centerRaDeg = viewRa.value, centerDecDeg = viewDec.value) => {
    const a0 = centerRaDeg * DEG2RAD;
    const d0 = centerDecDeg * DEG2RAD;
    const a = raDeg * DEG2RAD;
    const d = decDeg * DEG2RAD;
    const da = a - a0;

    const sinD0 = Math.sin(d0);
    const cosD0 = Math.cos(d0);
    const sinD = Math.sin(d);
    const cosD = Math.cos(d);
    const cosDa = Math.cos(da);

    const cosC = sinD0 * sinD + cosD0 * cosD * cosDa;
    if (cosC <= -0.35) return null;

    const k = 2.0 / (1.0 + cosC);
    const f = getFocalLengthPx();

    // East (+RA) maps to -X (left); North (+Dec) maps to -Y (up)
    const x = viewportW.value * 0.5 - f * k * cosD * Math.sin(da);
    const y = viewportH.value * 0.5 - f * k * (cosD0 * sinD - sinD0 * cosD * cosDa);

    return { x, y, k, cosC, f };
};

const screenToSky = (px, py, centerRaDeg = viewRa.value, centerDecDeg = viewDec.value) => {
    const f = getFocalLengthPx();
    const u = -(px - viewportW.value * 0.5) / f; // +East
    const v = -(py - viewportH.value * 0.5) / f; // +North
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

const computeLocalNorthAngleRad = (raDeg, decDeg, projPoint) => {
    const p = projPoint || projectSkyToScreen(raDeg, decDeg);
    if (!p) return 0;
    const step = decDeg > 89.0 ? -0.05 : 0.05;
    const pn = projectSkyToScreen(raDeg, decDeg + step);
    if (!pn) return 0;
    const dx = step > 0 ? pn.x - p.x : p.x - pn.x;
    const dy = step > 0 ? pn.y - p.y : p.y - pn.y;
    // Angle of local North vector relative to screen Up (0, -1), clockwise positive
    return Math.atan2(dx, -dy);
};

// --- CONSTELLATION LOOKUP FOR STATUS BAR ---
const findConstellationName = (raDeg, decDeg) => {
    if (!skyDataCache) return '';
    const bounds = skyDataCache.constellation_bounds || [];
    const consts = skyDataCache.constellations || [];
    const nameById = new Map();
    for (const c of consts) {
        nameById.set(c.id, c.name);
    }

    // Local gnomonic point-in-polygon around (raDeg, decDeg)
    const a0 = raDeg * DEG2RAD;
    const d0 = decDeg * DEG2RAD;
    const sinD0 = Math.sin(d0);
    const cosD0 = Math.cos(d0);

    for (const entry of bounds) {
        const rings = entry.rings || [];
        for (const ring of rings) {
            if (!ring || ring.length < 3) continue;
            // Quick check using first vertex angular distance
            const [r0, dc0] = ring[0];
            if (angularDistanceDeg(raDeg, decDeg, r0, dc0) > 65) continue;

            let inside = false;
            let valid = true;
            const pts = [];
            for (let i = 0; i < ring.length; i++) {
                const a = ring[i][0] * DEG2RAD;
                const d = ring[i][1] * DEG2RAD;
                const da = a - a0;
                const sinD = Math.sin(d);
                const cosD = Math.cos(d);
                const cosC = sinD0 * sinD + cosD0 * cosD * Math.cos(da);
                if (cosC <= 0.05) {
                    valid = false;
                    break;
                }
                pts.push([
                    (cosD * Math.sin(da)) / cosC,
                    (cosD0 * sinD - sinD0 * cosD * Math.cos(da)) / cosC
                ]);
            }
            if (!valid) continue;

            for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
                const xi = pts[i][0], yi = pts[i][1];
                const xj = pts[j][0], yj = pts[j][1];
                const intersect =
                    yi > 0 !== yj > 0 &&
                    0 < ((xj - xi) * (0 - yi)) / (yj - yi + 1e-15) + xi;
                if (intersect) inside = !inside;
            }
            if (inside) {
                return nameById.get(entry.id) || entry.id;
            }
        }
    }

    // Fallback: closest constellation centroid
    let bestName = '';
    let bestDist = Infinity;
    for (const c of consts) {
        const dist = angularDistanceDeg(raDeg, decDeg, c.ra, c.dec);
        if (dist < bestDist) {
            bestDist = dist;
            bestName = c.name;
        }
    }
    return bestName;
};

// --- COMBINED CACHED SKY IMAGES ---
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

    // 1. Server-scanned cached sky images across all setups
    for (const img of cachedServerImages.value) {
        if (!img?.url || img.ra === undefined || img.dec === undefined) continue;
        const cleanUrl = String(img.url).split('?')[0];
        const key = `${Number(img.ra).toFixed(3)}_${Number(img.dec).toFixed(3)}_${Number(img.fov).toFixed(2)}`;
        byKey.set(key, {
            name: img.name,
            commonName: img.common_name || '',
            ra: Number(img.ra),
            dec: Number(img.dec),
            fov: Math.max(Number(img.fov) || 1.0, 0.2),
            url: cleanUrl,
            isActiveTarget: Boolean(props.object && img.name === props.object.name),
            isCustom: Boolean(img.is_custom)
        });
    }

    // 2. Streamed catalog objects with live cached status
    if (Array.isArray(props.objects)) {
        for (const o of props.objects) {
            if (!o || o.status !== 'cached' || !o.image_url) continue;
            const ra = Number(o.center_ra !== undefined ? o.center_ra : o.ra);
            const dec = Number(o.center_dec !== undefined ? o.center_dec : o.dec);
            const fov = Math.max(Number(o.image_fov || props.baseImageFov || 1.0), 0.2);
            if (!Number.isFinite(ra) || !Number.isFinite(dec)) continue;
            const key = `${ra.toFixed(3)}_${dec.toFixed(3)}_${fov.toFixed(2)}`;
            byKey.set(key, {
                name: o.name,
                commonName: o.common_name && o.common_name !== 'N/A' ? o.common_name : '',
                ra,
                dec,
                fov,
                url: String(o.image_url),
                isActiveTarget: Boolean(props.object && o.name === props.object.name),
                isCustom: Boolean(o.custom_framed)
            });
        }
    }

    // 3. Active selected object's current image (highest priority)
    if (props.object?.image_url) {
        const ra = Number(
            props.object.center_ra !== undefined ? props.object.center_ra : props.object.ra
        );
        const dec = Number(
            props.object.center_dec !== undefined ? props.object.center_dec : props.object.dec
        );
        const fov = Math.max(
            Number(props.object.image_fov || props.baseImageFov || 1.0),
            0.2
        );
        if (Number.isFinite(ra) && Number.isFinite(dec)) {
            const key = `${ra.toFixed(3)}_${dec.toFixed(3)}_${fov.toFixed(2)}`;
            byKey.set(key, {
                name: props.object.name,
                commonName:
                    props.object.common_name && props.object.common_name !== 'N/A'
                        ? props.object.common_name
                        : '',
                ra,
                dec,
                fov,
                url: String(props.object.image_url),
                isActiveTarget: true,
                isCustom: Boolean(props.object.custom_framed)
            });
        }
    }

    // Sort widest FOV first, with active target rendered last (on top of background cutouts)
    return Array.from(byKey.values()).sort((a, b) => {
        if (a.isActiveTarget !== b.isActiveTarget) return a.isActiveTarget ? 1 : -1;
        return b.fov - a.fov;
    });
});

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

// --- STATUS BAR READOUT ---
const readoutData = computed(() => {
    const pt = cursorSky.value || { ra: viewRa.value, dec: viewDec.value };
    const constName = findConstellationName(pt.ra, pt.dec);
    const fovVal = mapFovDeg.value;
    const fovText = fovVal >= 10 ? `${Math.round(fovVal)}°` : `${fovVal.toFixed(1)}°`;
    return {
        raText: formatRaHms(pt.ra),
        decText: formatDecDms(pt.dec),
        constellation: constName,
        fovText
    };
});

const isOffTarget = computed(() => {
    const tgt = getTargetCoords();
    return angularDistanceDeg(viewRa.value, viewDec.value, tgt.ra, tgt.dec) > 0.35;
});

// --- CANVAS RENDERING ---
const scheduleDraw = () => {
    if (rafId !== null) return;
    rafId = requestAnimationFrame(() => {
        rafId = null;
        drawSkyCanvas();
    });
};

const drawPolylineOnSky = (ctx, points, maxJumpPx) => {
    let started = false;
    let prev = null;
    for (let i = 0; i < points.length; i++) {
        const p = projectSkyToScreen(points[i][0], points[i][1]);
        if (!p || p.cosC <= -0.1) {
            started = false;
            prev = null;
            continue;
        }
        if (!started) {
            ctx.moveTo(p.x, p.y);
            started = true;
        } else {
            const dist = Math.hypot(p.x - prev.x, p.y - prev.y);
            if (dist > maxJumpPx) {
                ctx.moveTo(p.x, p.y);
            } else {
                ctx.lineTo(p.x, p.y);
            }
        }
        prev = p;
    }
};

const drawSkyCanvas = () => {
    const canvas = canvasRef.value;
    if (!canvas) return;
    const w = viewportW.value;
    const h = viewportH.value;
    if (w <= 2 || h <= 2) return;

    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const targetW = Math.round(w * dpr);
    const targetH = Math.round(h * dpr);
    if (canvas.width !== targetW || canvas.height !== targetH) {
        canvas.width = targetW;
        canvas.height = targetH;
    }

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    ctx.save();
    ctx.scale(dpr, dpr);

    // 1. Deep night sky background gradient
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
    ctx.fillStyle = bgGrad;
    ctx.fillRect(0, 0, w, h);

    interactiveItems = [];
    const placedBoxes = [];

    const overlapsPlaced = (box, pad = 3) => {
        for (let i = 0; i < placedBoxes.length; i++) {
            const b = placedBoxes[i];
            if (
                box.x1 - pad < b.x2 &&
                box.x2 + pad > b.x1 &&
                box.y1 - pad < b.y2 &&
                box.y2 + pad > b.y1
            ) {
                return true;
            }
        }
        return false;
    };

    const maxSegJump = Math.max(w, h) * 1.4;
    const fov = mapFovDeg.value;
    const maxVisAngle = Math.min(115, Math.max(fov * 1.35, 18));

    // 2. Milky Way diffuse contours
    if (skyDataCache?.milky_way) {
        const mwAlphas = [0.032, 0.028, 0.026];
        for (const layer of skyDataCache.milky_way) {
            const lvl = layer.level || 0;
            ctx.fillStyle = `rgba(148, 174, 230, ${mwAlphas[lvl] || 0.025})`;
            for (const poly of layer.polygons || []) {
                const outer = poly[0];
                if (!outer || outer.length < 3) continue;

                // Ensure entire ring is on visible hemisphere to prevent horizon inversion fill artifacts
                let allFront = true;
                const projectedRings = [];
                for (const ring of poly) {
                    const pts = [];
                    for (let i = 0; i < ring.length; i++) {
                        const p = projectSkyToScreen(ring[i][0], ring[i][1]);
                        if (!p || p.cosC < 0.02) {
                            allFront = false;
                            break;
                        }
                        pts.push(p);
                    }
                    if (!allFront) break;
                    projectedRings.push(pts);
                }
                if (!allFront || projectedRings.length === 0) continue;

                ctx.beginPath();
                for (const pts of projectedRings) {
                    if (pts.length < 3) continue;
                    ctx.moveTo(pts[0].x, pts[0].y);
                    for (let i = 1; i < pts.length; i++) {
                        ctx.lineTo(pts[i].x, pts[i].y);
                    }
                    ctx.closePath();
                }
                ctx.fill('evenodd');
            }
        }
    }

    // 3. Curved Celestial RA/Dec Grid
    if (showGrid.value) {
        let raStepDeg = 15.0; // 1h
        let decStepDeg = 10.0;
        if (fov > 60) {
            raStepDeg = 30.0;
            decStepDeg = 15.0;
        } else if (fov > 24) {
            raStepDeg = 15.0;
            decStepDeg = 10.0;
        } else if (fov > 10) {
            raStepDeg = 7.5;
            decStepDeg = 5.0;
        } else if (fov > 4) {
            raStepDeg = 3.75;
            decStepDeg = 2.0;
        } else {
            raStepDeg = 1.25;
            decStepDeg = 1.0;
        }

        ctx.lineWidth = 1;
        // Meridians (constant RA)
        ctx.strokeStyle = 'rgba(92, 128, 184, 0.14)';
        ctx.beginPath();
        const decSampleStep = fov > 25 ? 2.5 : 0.8;
        for (let ra = 0; ra < 360 - 1e-5; ra += raStepDeg) {
            // Quick cull by RA angular separation near viewDec
            let dRa = Math.abs(ra - viewRa.value);
            if (dRa > 180) dRa = 360 - dRa;
            if (Math.abs(viewDec.value) < 68 && dRa * Math.cos(viewDec.value * DEG2RAD) > maxVisAngle) {
                continue;
            }
            let started = false;
            let prev = null;
            for (let dec = -85; dec <= 85.001; dec += decSampleStep) {
                if (Math.abs(dec - viewDec.value) > maxVisAngle + 15 && Math.abs(viewDec.value) < 70) {
                    started = false;
                    continue;
                }
                const p = projectSkyToScreen(ra, dec);
                if (!p || p.cosC <= 0.02) {
                    started = false;
                    prev = null;
                    continue;
                }
                if (!started || (prev && Math.hypot(p.x - prev.x, p.y - prev.y) > maxSegJump)) {
                    ctx.moveTo(p.x, p.y);
                    started = true;
                } else {
                    ctx.lineTo(p.x, p.y);
                }
                prev = p;
            }
        }
        ctx.stroke();

        // Parallels (constant Dec)
        const raSampleStep = fov > 25 ? 3.0 : 1.0;
        for (let dec = -80; dec <= 80.001; dec += decStepDeg) {
            const isEquator = Math.abs(dec) < 0.01;
            ctx.strokeStyle = isEquator
                ? 'rgba(130, 175, 238, 0.25)'
                : 'rgba(92, 128, 184, 0.14)';
            ctx.beginPath();
            let started = false;
            let prev = null;
            for (let ra = 0; ra <= 360.001; ra += raSampleStep) {
                const rWrap = ra >= 360 ? 0 : ra;
                const p = projectSkyToScreen(rWrap, dec);
                if (!p || p.cosC <= 0.02) {
                    started = false;
                    prev = null;
                    continue;
                }
                if (!started || (prev && Math.hypot(p.x - prev.x, p.y - prev.y) > maxSegJump)) {
                    ctx.moveTo(p.x, p.y);
                    started = true;
                } else {
                    ctx.lineTo(p.x, p.y);
                }
                prev = p;
            }
            ctx.stroke();
        }
    }

    // 4. Constellation Boundaries & Stick Lines
    if (showConstellations.value && skyDataCache) {
        // Subtle IAU boundaries at medium/wide zoom
        if (fov >= 8 && skyDataCache.constellation_bounds) {
            ctx.save();
            ctx.strokeStyle = 'rgba(94, 126, 175, 0.11)';
            ctx.lineWidth = 0.85;
            ctx.setLineDash([4, 5]);
            ctx.beginPath();
            for (const b of skyDataCache.constellation_bounds) {
                for (const ring of b.rings || []) {
                    if (!ring || ring.length < 2) continue;
                    if (angularDistanceDeg(viewRa.value, viewDec.value, ring[0][0], ring[0][1]) > maxVisAngle + 35) {
                        continue;
                    }
                    drawPolylineOnSky(ctx, ring, maxSegJump);
                }
            }
            ctx.stroke();
            ctx.restore();
        }

        // Constellation stick lines
        if (skyDataCache.constellation_lines) {
            ctx.save();
            ctx.strokeStyle = 'rgba(116, 168, 242, 0.46)';
            ctx.lineWidth = 1.15;
            ctx.beginPath();
            for (const c of skyDataCache.constellation_lines) {
                for (const seg of c.lines || []) {
                    if (!seg || seg.length < 2) continue;
                    if (angularDistanceDeg(viewRa.value, viewDec.value, seg[0][0], seg[0][1]) > maxVisAngle + 30) {
                        continue;
                    }
                    drawPolylineOnSky(ctx, seg, maxSegJump);
                }
            }
            ctx.stroke();
            ctx.restore();
        }
    }

    // 5. Already-Downloaded Sky Survey Photos Projected at True Celestial Scale!
    if (showCachedPhotos.value && allSkyPhotos.value.length > 0) {
        for (const photo of allSkyPhotos.value) {
            const dist = angularDistanceDeg(viewRa.value, viewDec.value, photo.ra, photo.dec);
            if (dist > maxVisAngle) continue;

            const p = projectSkyToScreen(photo.ra, photo.dec);
            if (!p) continue;
            if (p.x < -w * 0.6 || p.x > w * 1.6 || p.y < -h * 0.6 || p.y > h * 1.6) continue;

            const sizePx = photo.fov * DEG2RAD * p.f * p.k;
            const northAngle = computeLocalNorthAngleRad(photo.ra, photo.dec, p);
            const isHovered =
                hoveredItem.value?.kind === 'photo' &&
                hoveredItem.value?.name === photo.name &&
                Math.abs(hoveredItem.value.ra - photo.ra) < 0.01;

            // Load image texture when visible enough to see (> 3.5 px)
            const imgEl = sizePx >= 3.5 ? ensureImageLoaded(photo.url) : null;

            ctx.save();
            ctx.translate(p.x, p.y);
            ctx.rotate(northAngle);

            if (imgEl && sizePx >= 5) {
                const half = sizePx * 0.5;
                // Subtle dark backing + screen blend so sky survey nebulosity glows naturally on the star map
                ctx.fillStyle = 'rgba(4, 7, 14, 0.62)';
                ctx.fillRect(-half, -half, sizePx, sizePx);

                ctx.save();
                ctx.globalAlpha = photo.isActiveTarget ? 0.96 : 0.88;
                ctx.globalCompositeOperation = 'screen';
                ctx.drawImage(imgEl, -half, -half, sizePx, sizePx);
                ctx.restore();

                // Crisp subtle photo footprint border
                ctx.strokeStyle = photo.isActiveTarget
                    ? 'rgba(251, 191, 36, 0.58)'
                    : isHovered
                      ? 'rgba(56, 189, 248, 0.9)'
                      : 'rgba(56, 189, 248, 0.42)';
                ctx.lineWidth = isHovered ? 1.6 : 1.0;
                ctx.strokeRect(-half, -half, sizePx, sizePx);
            } else if (!photo.isActiveTarget) {
                // At ultra-wide FOV where a 0.9° photo is tiny, draw a clean glowing photo micro-tile
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

            // Register non-active downloaded photos for click/hover navigation
            if (!photo.isActiveTarget) {
                const hitRadius = Math.max(sizePx * 0.5, 9);
                const matchObj = Array.isArray(props.objects)
                    ? props.objects.find((o) => o.name === photo.name)
                    : null;
                interactiveItems.push({
                    kind: 'photo',
                    name: photo.name,
                    commonName: photo.commonName,
                    fov: photo.fov,
                    ra: photo.ra,
                    dec: photo.dec,
                    x: p.x,
                    y: p.y,
                    hitR: hitRadius,
                    refObj: matchObj,
                    isCustom: photo.isCustom
                });
            }
        }
    }

    // Reserve label space for Active Target Pill Badge first so stars/DSOs never collide with it
    let activeTargetScreen = null;
    if (props.object) {
        const tRa = parseFloat(props.object.ra);
        const tDec = parseFloat(props.object.dec);
        if (Number.isFinite(tRa) && Number.isFinite(tDec)) {
            const tp = projectSkyToScreen(tRa, tDec);
            if (tp && tp.x >= -100 && tp.x <= w + 100 && tp.y >= -100 && tp.y <= h + 100) {
                activeTargetScreen = { ...tp, ra: tRa, dec: tDec };
                const common =
                    props.object.common_name && props.object.common_name !== 'N/A'
                        ? ` · ${props.object.common_name}`
                        : '';
                const badgeText = `${props.object.name}${common}`;
                ctx.font = '600 12.5px system-ui, -apple-system, sans-serif';
                const tw = ctx.measureText(badgeText).width + 22;
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

    // 6. Stars (Magnitude-scaled + Realistic B-V Color + Bright Star Glow + Names)
    if (skyDataCache?.stars) {
        const magLimit = fov > 55 ? 5.35 : fov > 28 ? 5.75 : 6.05;
        const nameMagLimit = fov > 60 ? 2.1 : fov > 32 ? 2.75 : fov > 14 ? 3.5 : 4.4;

        ctx.font = '500 11.5px system-ui, -apple-system, sans-serif';
        ctx.textBaseline = 'middle';

        for (let i = 0; i < skyDataCache.stars.length; i++) {
            const st = skyDataCache.stars[i];
            const mag = st[2];
            if (mag > magLimit) break; // Stars array is pre-sorted brightest first!

            const ra = st[0];
            const dec = st[1];
            if (Math.abs(dec - viewDec.value) > maxVisAngle && Math.abs(viewDec.value) < 68) {
                continue;
            }
            const p = projectSkyToScreen(ra, dec);
            if (!p) continue;
            if (p.x < -20 || p.x > w + 20 || p.y < -20 || p.y > h + 20) continue;

            const [r, g, b] = bvToRgb(st[3]);
            const radius = Math.max(0.55, Math.min(4.2, (5.9 - mag) * 0.56));
            const alpha = Math.max(0.28, Math.min(1.0, (6.3 - mag) / 4.2));

            // Soft radial glow halo for bright stars (Sirius, Rigel, Betelgeuse, etc.)
            if (mag < 2.3) {
                const haloR = radius * (mag < 0.8 ? 4.4 : 3.1);
                const halo = ctx.createRadialGradient(p.x, p.y, radius * 0.3, p.x, p.y, haloR);
                halo.addColorStop(0, `rgba(${r}, ${g}, ${b}, ${mag < 0.8 ? 0.5 : 0.34})`);
                halo.addColorStop(1, `rgba(${r}, ${g}, ${b}, 0)`);
                ctx.fillStyle = halo;
                ctx.beginPath();
                ctx.arc(p.x, p.y, haloR, 0, Math.PI * 2);
                ctx.fill();
            }

            ctx.fillStyle = `rgba(${r}, ${g}, ${b}, ${alpha.toFixed(3)})`;
            ctx.beginPath();
            ctx.arc(p.x, p.y, radius, 0, Math.PI * 2);
            ctx.fill();

            // Proper star label (e.g., Betelgeuse, Bellatrix, Alnitak, Sirius)
            const starName = st[4] || (fov < 12 && mag < 3.8 ? st[5] : '');
            if (starName && mag <= nameMagLimit && p.x > 30 && p.x < w - 65 && p.y > 24 && p.y < h - 30) {
                const tw = ctx.measureText(starName).width;
                const lx = p.x + radius + 5;
                const ly = p.y;
                const box = { x1: lx - 2, y1: ly - 7, x2: lx + tw + 2, y2: ly + 7 };
                if (!overlapsPlaced(box, 2)) {
                    placedBoxes.push(box);
                    ctx.fillStyle = 'rgba(3, 6, 12, 0.75)';
                    ctx.fillText(starName, lx + 1, ly + 1);
                    ctx.fillStyle = mag < 1.6 ? 'rgba(232, 240, 252, 0.92)' : 'rgba(196, 210, 232, 0.78)';
                    ctx.fillText(starName, lx, ly);
                }
            }
        }
    }

    // 7. Nearby Deep-Sky Objects (Messier & prominent catalog objects + cached photo labels)
    if (showDsos.value && Array.isArray(props.objects)) {
        const candidates = [];
        for (const obj of props.objects) {
            if (!obj || (props.object && obj.name === props.object.name)) continue;
            const ra = parseFloat(obj.ra);
            const dec = parseFloat(obj.dec);
            if (!Number.isFinite(ra) || !Number.isFinite(dec)) continue;
            if (Math.abs(dec - viewDec.value) > maxVisAngle && Math.abs(viewDec.value) < 68) {
                continue;
            }
            const isMessier = /^M\s*\d+$/i.test(obj.name);
            const mag = typeof obj.mag === 'number' && obj.mag < 90 ? obj.mag : 13.5;
            const maj = typeof obj.maj_ax === 'number' ? obj.maj_ax : 0;
            const isCached =
                obj.status === 'cached' ||
                allSkyPhotos.value.some((ph) => ph.name === obj.name);

            // Filter by zoom level so wide constellation view stays clean like the reference screenshot
            if (fov > 35 && !isMessier && !isCached && mag > 7.2 && maj < 30) continue;
            if (fov > 16 && !isMessier && !isCached && mag > 9.0 && maj < 15) continue;

            const p = projectSkyToScreen(ra, dec);
            if (!p || p.x < 10 || p.x > w - 10 || p.y < 14 || p.y > h - 18) continue;

            candidates.push({
                obj,
                ra,
                dec,
                p,
                isMessier,
                isCached,
                score: (isCached ? 200 : 0) + (isMessier ? 100 : 0) + Math.min(maj, 60) - mag * 3
            });
        }

        candidates.sort((a, b) => b.score - a.score);
        const maxMarkers = fov > 35 ? 28 : fov > 15 ? 42 : 65;

        ctx.font = '500 11px system-ui, -apple-system, sans-serif';
        ctx.textBaseline = 'middle';

        for (let i = 0; i < Math.min(candidates.length, maxMarkers); i++) {
            const c = candidates[i];
            const { p, obj, isCached } = c;
            const majDeg = (typeof obj.maj_ax === 'number' ? obj.maj_ax : 0) / 60.0;
            const rPx = Math.max(3.4, Math.min(22, majDeg * DEG2RAD * p.f * p.k * 0.5));
            const isHovered =
                hoveredItem.value?.name === obj.name && hoveredItem.value?.kind !== 'active';

            ctx.save();
            ctx.beginPath();
            ctx.arc(p.x, p.y, rPx, 0, Math.PI * 2);
            ctx.strokeStyle = isHovered
                ? '#38bdf8'
                : isCached
                  ? 'rgba(56, 189, 248, 0.85)'
                  : 'rgba(167, 139, 250, 0.78)';
            ctx.lineWidth = isHovered ? 1.8 : 1.2;
            ctx.stroke();
            ctx.restore();

            // Compact label (e.g. "M78", "M42", "NGC 2244")
            const shortName = /^M\s+(\d+)$/i.test(obj.name)
                ? obj.name.replace(/\s+/g, '')
                : obj.name;
            const lx = p.x + rPx + 4.5;
            const ly = p.y;
            const tw = ctx.measureText(shortName).width;
            const box = { x1: lx - 2, y1: ly - 6.5, x2: lx + tw + 2, y2: ly + 6.5 };

            if (!overlapsPlaced(box, 2)) {
                placedBoxes.push(box);
                ctx.fillStyle = 'rgba(3, 6, 12, 0.78)';
                ctx.fillText(shortName, lx + 1, ly + 1);
                ctx.fillStyle = isCached
                    ? 'rgba(125, 211, 252, 0.94)'
                    : 'rgba(186, 164, 252, 0.88)';
                ctx.fillText(shortName, lx, ly);
            }

            interactiveItems.push({
                kind: 'dso',
                name: obj.name,
                commonName: obj.common_name && obj.common_name !== 'N/A' ? obj.common_name : '',
                fov: obj.image_fov || null,
                ra: c.ra,
                dec: c.dec,
                x: p.x,
                y: p.y,
                hitR: Math.max(rPx + 5, 10),
                refObj: obj
            });
        }
    }

    // 8. Constellation Names (Tracked uppercase background typography: ORION, MONOCEROS, LEPUS)
    if (showConstellations.value && fov >= 7 && skyDataCache?.constellations) {
        ctx.save();
        ctx.font = '600 11px system-ui, -apple-system, sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';

        for (const c of skyDataCache.constellations) {
            if (fov < 14 && c.rank > 2) continue;
            if (angularDistanceDeg(viewRa.value, viewDec.value, c.ra, c.dec) > maxVisAngle) {
                continue;
            }
            const p = projectSkyToScreen(c.ra, c.dec);
            if (!p || p.x < 55 || p.x > w - 55 || p.y < 36 || p.y > h - 36) continue;

            const tracked = String(c.name).toUpperCase().split('').join(' ');
            const tw = ctx.measureText(tracked).width;
            const box = {
                x1: p.x - tw * 0.5 - 4,
                y1: p.y - 8,
                x2: p.x + tw * 0.5 + 4,
                y2: p.y + 8
            };
            if (!overlapsPlaced(box, 3)) {
                placedBoxes.push(box);
                ctx.fillStyle = 'rgba(3, 6, 12, 0.65)';
                ctx.fillText(tracked, p.x + 1, p.y + 1);
                ctx.fillStyle = 'rgba(138, 168, 210, 0.58)';
                ctx.fillText(tracked, p.x, p.y);
            }
        }
        ctx.restore();
    }

    // 9. Active Target Highlight Ring, Rotated Sensor FOV Frame & Comparison Setups, and Floating Badge
    if (props.object) {
        const sensorCoords = getTargetCoords();
        const sp = projectSkyToScreen(sensorCoords.ra, sensorCoords.dec);

        // Draw Comparison Equipment FOV Rectangles at true celestial scale
        if (sp && Array.isArray(props.comparisonRectangles)) {
            const localNorth = computeLocalNorthAngleRad(sensorCoords.ra, sensorCoords.dec, sp);
            const totalRotRad = localNorth + (Number(props.rotation) || 0) * DEG2RAD;

            for (const comp of props.comparisonRectangles) {
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

        // Draw Active Telescope + Camera Sensor FOV Rectangle
        if (sp && props.sensorFov?.w > 0 && props.sensorFov?.h > 0) {
            const localNorth = computeLocalNorthAngleRad(sensorCoords.ra, sensorCoords.dec, sp);
            const totalRotRad = localNorth + (Number(props.rotation) || 0) * DEG2RAD;
            const sw = props.sensorFov.w * DEG2RAD * sp.f * sp.k;
            const sh = props.sensorFov.h * DEG2RAD * sp.f * sp.k;

            ctx.save();
            ctx.translate(sp.x, sp.y);
            ctx.rotate(totalRotRad);

            const drawW = Math.max(sw, 6);
            const drawH = Math.max(sh, 4.5);
            ctx.strokeStyle = '#f59e0b';
            ctx.shadowColor = 'rgba(245, 158, 11, 0.55)';
            ctx.shadowBlur = 8;
            ctx.lineWidth = 1.9;
            ctx.strokeRect(-drawW * 0.5, -drawH * 0.5, drawW, drawH);
            ctx.restore();
        }

        // Draw Outer Target Circle + Warm Amber Label Pill (like "M 42 · Orion Nebula")
        if (activeTargetScreen) {
            const tp = activeTargetScreen;
            const baseImgPx =
                (props.object.image_fov || props.baseImageFov || 1.0) * DEG2RAD * tp.f * tp.k;
            const ringR = Math.max(baseImgPx * 0.58, 15);

            ctx.save();
            ctx.beginPath();
            ctx.arc(tp.x, tp.y, ringR, 0, Math.PI * 2);
            ctx.strokeStyle = 'rgba(245, 158, 11, 0.55)';
            ctx.lineWidth = 1.25;
            ctx.stroke();

            // Pill badge above target
            const common =
                props.object.common_name && props.object.common_name !== 'N/A'
                    ? ` · ${props.object.common_name}`
                    : '';
            const badgeText = `${props.object.name}${common}`;
            ctx.font = '600 12.5px system-ui, -apple-system, sans-serif';
            const textW = ctx.measureText(badgeText).width;
            const pillW = textW + 20;
            const pillH = 24;
            const px = Math.max(12, Math.min(w - pillW - 12, tp.x - pillW * 0.5));
            const py = tp.y - ringR - pillH - 8;

            if (py > 8) {
                ctx.fillStyle = 'rgba(14, 13, 12, 0.88)';
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
                ctx.fillText(badgeText, px + pillW * 0.5, py + pillH * 0.5 + 0.5);
            }
            ctx.restore();

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

    ctx.restore();
};

// --- INTERACTION HANDLERS (Pan, Wheel Zoom, Touch Pinch, Click/Hover) ---
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
        scheduleDraw();
        return;
    }

    const startTime = performance.now();
    const duration = 260;
    const step = (now) => {
        const t = Math.min(1, (now - startTime) / duration);
        const ease = 1 - Math.pow(1 - t, 3);
        viewRa.value = wrapRaDeg(startRa + dRa * ease);
        viewDec.value = clampDecDeg(startDec + dDec * ease);
        scheduleDraw();
        if (t < 1) {
            animFrameId = requestAnimationFrame(step);
        } else {
            animFrameId = null;
        }
    };
    animFrameId = requestAnimationFrame(step);
};

const applyZoomFactor = (factor, anchorPx = null, anchorPy = null) => {
    const prevFov = mapFovDeg.value;
    const minMapFov = Math.min(0.35, (props.baseImageFov || 1.0) * 0.8);
    const nextFov = Math.max(minMapFov, Math.min(105.0, prevFov * factor));

    // Seamless transition back into close-up Framing viewport when zooming in to target image scale
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

    // Keep the sky point under the cursor anchored while zooming
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
    let bestDist = Infinity;
    for (const item of interactiveItems) {
        const d = Math.hypot(px - item.x, py - item.y);
        if (d <= item.hitR && d < bestDist) {
            best = item;
            bestDist = d;
        }
    }
    return best;
};

const onMouseDown = (e) => {
    if (e.button !== 0) return;
    isPanning.value = true;
    panMoved = false;
    panStartScreen = { x: e.clientX, y: e.clientY };
    panStartSky = { ra: viewRa.value, dec: viewDec.value };
};

const onMouseMove = (e) => {
    const rect = canvasRef.value?.getBoundingClientRect();
    if (!rect) return;
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;

    if (isPanning.value) {
        const dx = e.clientX - panStartScreen.x;
        const dy = e.clientY - panStartScreen.y;
        if (Math.hypot(dx, dy) > 3) panMoved = true;

        // Convert screen drag delta into sky center shift
        const shiftedSky = screenToSky(
            viewportW.value * 0.5 - dx,
            viewportH.value * 0.5 - dy,
            panStartSky.ra,
            panStartSky.dec
        );
        viewRa.value = shiftedSky.ra;
        viewDec.value = shiftedSky.dec;
        cursorSky.value = screenToSky(px, py);
        scheduleDraw();
        return;
    }

    cursorSky.value = screenToSky(px, py);
    const hit = findHitItem(px, py);
    if (hit?.name !== hoveredItem.value?.name || hit?.kind !== hoveredItem.value?.kind) {
        hoveredItem.value = hit;
        scheduleDraw();
    } else if (hit) {
        hoveredItem.value = hit;
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
    // Custom coordinate photo without catalog object: smoothly center on it
    viewRa.value = wrapRaDeg(item.ra);
    viewDec.value = clampDecDeg(item.dec);
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
    }
};

const onMouseLeave = () => {
    isPanning.value = false;
    cursorSky.value = null;
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
        const dx = t.clientX - panStartScreen.x;
        const dy = t.clientY - panStartScreen.y;
        if (Math.hypot(dx, dy) > 4) panMoved = true;
        const shiftedSky = screenToSky(
            viewportW.value * 0.5 - dx,
            viewportH.value * 0.5 - dy,
            panStartSky.ra,
            panStartSky.dec
        );
        viewRa.value = shiftedSky.ra;
        viewDec.value = shiftedSky.dec;
        scheduleDraw();
        e.preventDefault();
    }
};

const onTouchEnd = (e) => {
    if (e.touches.length === 0) {
        isPanning.value = false;
        pinchStartDist = 0;
    }
};

const onKeyDown = (e) => {
    if (e.key === 'Escape') {
        emit('close');
    }
};

// --- WATCHERS & LIFECYCLE ---
watch(
    () => props.object?.name,
    (newName, oldName) => {
        if (newName && newName !== oldName) {
            const tgt = getTargetCoords();
            viewRa.value = tgt.ra;
            viewDec.value = tgt.dec;
            scheduleDraw();
        }
    }
);

watch(
    () => [
        props.object?.image_url,
        props.object?.status,
        props.rotation,
        props.sensorFov,
        props.comparisonRectangles,
        showCachedPhotos.value,
        showConstellations.value,
        showGrid.value,
        showDsos.value
    ],
    () => {
        if (props.object?.status === 'cached') {
            fetchCachedSkyImages();
        }
        scheduleDraw();
    },
    { deep: true }
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
    scheduleDraw();
});

onUnmounted(() => {
    window.removeEventListener('keydown', onKeyDown);
    if (resizeObserver) resizeObserver.disconnect();
    if (rafId) cancelAnimationFrame(rafId);
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

        <!-- Top-Left Navigation Controls (matches reference < Photo & Centre on photo) -->
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
            <span class="coord-seg">RA {{ readoutData.raText }}</span>
            <span class="coord-seg">Dec {{ readoutData.decText }}</span>
            <span v-if="readoutData.constellation" class="coord-seg const-seg">
                {{ readoutData.constellation }}
            </span>
            <span class="coord-seg fov-seg">FOV {{ readoutData.fovText }}</span>
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

/* Top-Left Pill Buttons */
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
    background: rgba(13, 18, 28, 0.84);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    border: 1px solid rgba(148, 163, 184, 0.26);
    color: #e2e8f0;
    font-size: 0.8rem;
    font-weight: 500;
    line-height: 1.2;
    cursor: pointer;
    transition: all 0.15s ease;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.45);
}

.sky-pill-btn:hover {
    background: rgba(26, 35, 52, 0.94);
    border-color: rgba(148, 163, 184, 0.5);
    color: #ffffff;
}

.sky-pill-btn.primary-back {
    border-color: rgba(56, 189, 248, 0.42);
}

.sky-pill-btn.primary-back:hover {
    border-color: #38bdf8;
    background: rgba(14, 116, 144, 0.35);
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
    background: rgba(13, 18, 28, 0.78);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    border: 1px solid rgba(148, 163, 184, 0.2);
    color: #94a3b8;
    font-size: 0.74rem;
    font-weight: 500;
    line-height: 1.2;
    cursor: pointer;
    transition: all 0.15s ease;
}

.sky-layer-chip:hover {
    color: #e2e8f0;
    border-color: rgba(148, 163, 184, 0.42);
}

.sky-layer-chip.active {
    background: rgba(15, 32, 54, 0.88);
    border-color: rgba(56, 189, 248, 0.48);
    color: #e0f2fe;
}

.sky-zoom-stepper {
    display: inline-flex;
    border-radius: 7px;
    overflow: hidden;
    border: 1px solid rgba(148, 163, 184, 0.26);
    background: rgba(13, 18, 28, 0.84);
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
    background: rgba(56, 189, 248, 0.2);
    color: #ffffff;
}

/* Hover Tooltip */
.sky-hover-tooltip {
    position: absolute;
    pointer-events: none;
    background: rgba(10, 14, 24, 0.92);
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
    background: rgba(10, 15, 28, 0.88);
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
    background: rgba(6, 9, 16, 0.72);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
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
