import { ref, shallowRef } from 'vue';

const DEG2RAD = Math.PI / 180.0;
const RAD2DEG = 180.0 / Math.PI;

export const skyAtlasVersion = ref(0);
export const skyAtlasReady = ref(false);
export const skyAtlasSectorMode = ref(false);
export const skyAtlasMeta = shallowRef({
    available: false,
    version: 3,
    mode: 'sectors',
    count: 0,
    sheets: 0,
    tileSize: 256,
    generatedAt: ''
});
export const skyAtlasItems = shallowRef([]);
export const skyAtlasLod0Sectors = shallowRef([]);
export const skyAtlasLod1Sectors = shallowRef([]);
export const skyAtlasLod2Sectors = shallowRef([]);

// 48-patch spherical bounding-cone partition (6 Dec bands x 8 RA sectors) for O(1) LOD sector culling
const PATCH_DEC_BINS = 6;
const PATCH_RA_BINS = 8;
const PATCH_COUNT = PATCH_DEC_BINS * PATCH_RA_BINS;
const patchCx = new Float64Array(PATCH_COUNT);
const patchCy = new Float64Array(PATCH_COUNT);
const patchCz = new Float64Array(PATCH_COUNT);
const patchRadius = new Float64Array(PATCH_COUNT);
const patchCosR = new Float64Array(PATCH_COUNT);
const patchSinR = new Float64Array(PATCH_COUNT);

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
    const r = ((raDeg % 360) + 360) % 360;
    const dBin = Math.max(0, Math.min(PATCH_DEC_BINS - 1, Math.floor((decDeg + 90.0) / 30.0)));
    const rBin = Math.max(0, Math.min(PATCH_RA_BINS - 1, Math.floor(r / 45.0)));
    return dBin * PATCH_RA_BINS + rBin;
};

let loadPromise = null;
let sheetMetaByIndex = new Map();
let sheetImageCache = new Map();
let itemByNormName = new Map();
let decBuckets = new Map();
let sheetTouchClock = 0;

// Bounded async WebP sheet decode queue + LRU GPU ImageBitmap cache
const decodeQueue = [];
const queuedSheetSet = new Set();
let activeSheetDecodes = 0;
const MAX_CONCURRENT_SHEET_DECODES = 2;
const MAX_CACHED_DETAIL_SHEETS = 24;

const normalizeTargetKey = (name) => {
    if (!name) return '';
    return String(name)
        .trim()
        .toUpperCase()
        .replace(/[\s_-]+/g, '');
};

const angularSeparationDeg = (ra1, dec1, ra2, dec2) => {
    const a1 = ra1 * DEG2RAD;
    const d1 = dec1 * DEG2RAD;
    const a2 = ra2 * DEG2RAD;
    const d2 = dec2 * DEG2RAD;
    const cosC =
        Math.sin(d1) * Math.sin(d2) +
        Math.cos(d1) * Math.cos(d2) * Math.cos(a2 - a1);
    return Math.acos(Math.max(-1.0, Math.min(1.0, cosC))) * RAD2DEG;
};

/**
 * Orthonormal gnomonic tangent basis [T, E, N] matching backend/sky_atlas.py:_make_tangent_basis
 * bit-for-bit, including exact North (+90) and South (-90) celestial poles.
 */
export const makeTangentBasis = (raDeg, decDeg) => {
    if (decDeg >= 89.9999) {
        return {
            tx: 0.0, ty: 0.0, tz: 1.0,
            ex: 0.0, ey: 1.0, ez: 0.0,
            nx: -1.0, ny: 0.0, nz: 0.0
        };
    }
    if (decDeg <= -89.9999) {
        return {
            tx: 0.0, ty: 0.0, tz: -1.0,
            ex: 0.0, ey: 1.0, ez: 0.0,
            nx: 1.0, ny: 0.0, nz: 0.0
        };
    }
    const a = raDeg * DEG2RAD;
    const d = decDeg * DEG2RAD;
    const sa = Math.sin(a);
    const ca = Math.cos(a);
    const sd = Math.sin(d);
    const cd = Math.cos(d);
    return {
        tx: cd * ca, ty: cd * sa, tz: sd,
        ex: -sa, ey: ca, ez: 0.0,
        nx: -sd * ca, ny: -sd * sa, nz: cd
    };
};

const disposeSheetRecord = (rec) => {
    if (rec?.isBitmap && rec.img && typeof rec.img.close === 'function') {
        try {
            rec.img.close();
        } catch {
            // Ignore already-closed ImageBitmap
        }
    }
};

const disposeSheetCache = (cacheMap) => {
    if (!cacheMap) return;
    for (const rec of cacheMap.values()) {
        disposeSheetRecord(rec);
    }
};

const evictLruDetailSheets = () => {
    const readyDetails = [];
    for (const [idx, rec] of sheetImageCache.entries()) {
        if (!rec || rec.status !== 'ready') continue;
        const meta = sheetMetaByIndex.get(idx);
        if (!meta || meta.lod === 0) continue; // Keep LOD 0 wide-sky sheet pinned permanently
        readyDetails.push({ idx, rec });
    }
    if (readyDetails.length <= MAX_CACHED_DETAIL_SHEETS) return;

    readyDetails.sort((a, b) => (a.rec.lastTouch || 0) - (b.rec.lastTouch || 0));
    const excess = readyDetails.length - MAX_CACHED_DETAIL_SHEETS;
    for (let i = 0; i < excess; i++) {
        const victim = readyDetails[i];
        disposeSheetRecord(victim.rec);
        sheetImageCache.delete(victim.idx);
    }
};

const decodeSheetNow = (sheetIndex) => {
    const meta = sheetMetaByIndex.get(sheetIndex);
    if (!meta) return Promise.resolve(null);

    const existing = sheetImageCache.get(sheetIndex);
    if (existing && (existing.status === 'ready' || existing.promise)) {
        if (existing.status === 'ready') {
            existing.lastTouch = ++sheetTouchClock;
        }
        return existing.promise || Promise.resolve(existing.img);
    }

    const cacheRef = sheetImageCache;
    const record = existing || {
        img: null,
        status: 'loading',
        isBitmap: false,
        promise: null,
        lastTouch: ++sheetTouchClock
    };
    record.status = 'loading';
    record.lastTouch = ++sheetTouchClock;
    cacheRef.set(sheetIndex, record);

    const loadViaHtmlImage = () =>
        new Promise((resolve) => {
            const img = new Image();
            img.decoding = 'async';
            img.onload = () => {
                if (sheetImageCache !== cacheRef) {
                    resolve(null);
                    return;
                }
                record.img = img;
                record.isBitmap = false;
                record.status = 'ready';
                record.lastTouch = ++sheetTouchClock;
                evictLruDetailSheets();
                skyAtlasVersion.value++;
                resolve(img);
            };
            img.onerror = () => {
                if (sheetImageCache === cacheRef) {
                    record.status = 'error';
                }
                resolve(null);
            };
            img.src = meta.url;
        });

    record.promise = (async () => {
        if (typeof createImageBitmap === 'function') {
            try {
                const res = await fetch(meta.url);
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                const blob = await res.blob();
                const bitmap = await createImageBitmap(blob, {
                    premultiplyAlpha: 'premultiply',
                    colorSpaceConversion: 'none'
                });
                if (sheetImageCache !== cacheRef) {
                    bitmap.close();
                    return null;
                }
                record.img = bitmap;
                record.isBitmap = true;
                record.status = 'ready';
                record.lastTouch = ++sheetTouchClock;
                evictLruDetailSheets();
                skyAtlasVersion.value++;
                return bitmap;
            } catch {
                return loadViaHtmlImage();
            }
        }
        return loadViaHtmlImage();
    })();

    return record.promise;
};

const pumpSheetDecodeQueue = () => {
    while (activeSheetDecodes < MAX_CONCURRENT_SHEET_DECODES && decodeQueue.length > 0) {
        const nextIdx = decodeQueue.shift();
        queuedSheetSet.delete(nextIdx);
        if (!sheetMetaByIndex.has(nextIdx)) continue;
        const cur = sheetImageCache.get(nextIdx);
        if (cur && (cur.status === 'ready' || cur.status === 'loading' || cur.status === 'error')) {
            continue;
        }
        activeSheetDecodes++;
        decodeSheetNow(nextIdx).finally(() => {
            activeSheetDecodes = Math.max(0, activeSheetDecodes - 1);
            pumpSheetDecodeQueue();
        });
    }
};

const enqueueSheetDecode = (sheetIndex, highPriority = false) => {
    if (!sheetMetaByIndex.has(sheetIndex)) return;
    const cur = sheetImageCache.get(sheetIndex);
    if (cur) return;
    if (queuedSheetSet.has(sheetIndex)) {
        if (highPriority) {
            const pos = decodeQueue.indexOf(sheetIndex);
            if (pos > 0) {
                decodeQueue.splice(pos, 1);
                decodeQueue.unshift(sheetIndex);
            }
        }
        return;
    }
    queuedSheetSet.add(sheetIndex);
    if (highPriority) {
        decodeQueue.unshift(sheetIndex);
    } else {
        decodeQueue.push(sheetIndex);
    }
    pumpSheetDecodeQueue();
};

const prepareSectorList = (rawSectors, defaultTileSize, lodLevel, validSheets) => {
    const out = [];
    const byPatch = Array.from({ length: PATCH_COUNT }, () => []);
    out.byPatch = byPatch;
    let maxRadiusRad = 0.0;
    out.maxRadiusRad = 0.0;
    if (!Array.isArray(rawSectors)) return out;

    for (let i = 0; i < rawSectors.length; i++) {
        const sc = rawSectors[i];
        if (!sc) continue;

        let ra;
        let dec;
        let span;
        let s;
        let u;
        let v;
        let w;
        let h;

        if (Array.isArray(sc)) {
            ra = Number(sc[0]);
            dec = Number(sc[1]);
            span = Number(sc[2]);
            s = Number(sc[3]);
            u = Number(sc[4]) || 0;
            v = Number(sc[5]) || 0;
            w = Number(sc[6]) || defaultTileSize;
            h = Number(sc[7]) || defaultTileSize;
        } else {
            if (sc.ra === undefined || sc.dec === undefined) continue;
            ra = Number(sc.ra);
            dec = Number(sc.dec);
            span = Number(sc.span);
            s = Number(sc.s);
            u = Number(sc.u) || 0;
            v = Number(sc.v) || 0;
            w = Number(sc.w) || defaultTileSize;
            h = Number(sc.h) || defaultTileSize;
        }

        if (!Number.isFinite(ra) || !Number.isFinite(dec) || !Number.isFinite(span) || span <= 0) {
            continue;
        }
        if (!validSheets.has(s)) continue;

        const halfTan = Math.tan(0.5 * span * DEG2RAD);
        const basis = makeTangentBasis(ra, dec);
        const invEdgeLen = 1.0 / Math.sqrt(1.0 + halfTan * halfTan);
        const radiusRad = Math.atan(Math.SQRT2 * halfTan);
        if (radiusRad > maxRadiusRad) maxRadiusRad = radiusRad;
        const patchId = getSkyPatchId(ra, dec);

        const rec = {
            isSector: true,
            lod: lodLevel,
            patchId,
            ra,
            dec,
            span,
            halfTan,
            tx: basis.tx,
            ty: basis.ty,
            tz: basis.tz,
            // Left image edge (px = 0): +East (+xi)
            lx: (basis.tx + halfTan * basis.ex) * invEdgeLen,
            ly: (basis.ty + halfTan * basis.ey) * invEdgeLen,
            lz: (basis.tz + halfTan * basis.ez) * invEdgeLen,
            // Right image edge (px = w): -East (-xi)
            rx: (basis.tx - halfTan * basis.ex) * invEdgeLen,
            ry: (basis.ty - halfTan * basis.ey) * invEdgeLen,
            rz: (basis.tz - halfTan * basis.ez) * invEdgeLen,
            // Top image edge (py = 0): +North (+eta)
            topX: (basis.tx + halfTan * basis.nx) * invEdgeLen,
            topY: (basis.ty + halfTan * basis.ny) * invEdgeLen,
            topZ: (basis.tz + halfTan * basis.nz) * invEdgeLen,
            // Bottom image edge (py = h): -North (-eta)
            botX: (basis.tx - halfTan * basis.nx) * invEdgeLen,
            botY: (basis.ty - halfTan * basis.ny) * invEdgeLen,
            botZ: (basis.tz - halfTan * basis.nz) * invEdgeLen,
            radiusRad,
            cosR: Math.cos(radiusRad),
            sinR: Math.sin(radiusRad),
            s,
            u,
            v,
            w,
            h
        };
        out.push(rec);
        byPatch[patchId].push(rec);
    }
    out.maxRadiusRad = maxRadiusRad;
    return out;
};

const resetState = () => {
    const prevCache = sheetImageCache;
    decodeQueue.length = 0;
    queuedSheetSet.clear();
    sheetMetaByIndex = new Map();
    sheetImageCache = new Map();
    disposeSheetCache(prevCache);
    itemByNormName = new Map();
    decBuckets = new Map();
    skyAtlasItems.value = [];
    skyAtlasLod0Sectors.value = [];
    skyAtlasLod1Sectors.value = [];
    skyAtlasLod2Sectors.value = [];
    skyAtlasSectorMode.value = false;
    skyAtlasMeta.value = {
        available: false,
        version: 3,
        mode: 'sectors',
        count: 0,
        sheets: 0,
        tileSize: 256,
        generatedAt: ''
    };
    skyAtlasReady.value = false;
    skyAtlasVersion.value++;
};

const indexManifest = (data) => {
    const nextSheets = new Map();
    const rawSheets = Array.isArray(data?.sheets) ? data.sheets : [];
    const genTag = String(data?.generated_at || '');

    for (let i = 0; i < rawSheets.length; i++) {
        const sh = rawSheets[i];
        if (!sh || sh.index === undefined || !sh.file) continue;
        const idx = Number(sh.index);
        const query = genTag ? `?v=${encodeURIComponent(genTag)}` : '';
        nextSheets.set(idx, {
            index: idx,
            lod: sh.lod !== undefined ? Number(sh.lod) : 0,
            file: String(sh.file),
            url: `/sky_atlas/${sh.file}${query}`,
            width: Number(sh.width) || 4096,
            height: Number(sh.height) || 4096,
            tiles: Number(sh.tiles) || 0
        });
    }

    const prevCache = sheetImageCache;
    decodeQueue.length = 0;
    queuedSheetSet.clear();
    thumbUrlCache.clear();
    sheetMetaByIndex = nextSheets;
    sheetImageCache = new Map();
    disposeSheetCache(prevCache);

    const isSectorV2 = Number(data?.version) >= 2 || data?.mode === 'sectors';
    if (isSectorV2) {
        const rawLods = Array.isArray(data?.lods) ? data.lods : null;
        const lod0Node = data?.lod0 || (rawLods ? rawLods.find((l) => Number(l?.level) === 0) : null);
        const lod1Node = data?.lod1 || (rawLods ? rawLods.find((l) => Number(l?.level) === 1) : null);
        const lod2Node = data?.lod2 || (rawLods ? rawLods.find((l) => Number(l?.level) === 2) : null);

        const lod0TileSize = Number(lod0Node?.tile_size) || 256;
        const lod1TileSize = Number(lod1Node?.tile_size) || 512;
        const lod2TileSize = Number(lod2Node?.tile_size) || 512;
        const lod0Sectors = prepareSectorList(lod0Node?.sectors, lod0TileSize, 0, nextSheets);
        const lod1Sectors = prepareSectorList(lod1Node?.sectors, lod1TileSize, 1, nextSheets);
        const lod2Sectors = prepareSectorList(lod2Node?.sectors, lod2TileSize, 2, nextSheets);

        const hasAny = lod0Sectors.length > 0 || lod1Sectors.length > 0 || lod2Sectors.length > 0;
        const sourceCount =
            Number(data?.source_count) ||
            lod2Sectors.length ||
            lod1Sectors.length ||
            lod0Sectors.length;

        itemByNormName = new Map();
        decBuckets = new Map();
        skyAtlasItems.value = [];
        skyAtlasLod0Sectors.value = lod0Sectors;
        skyAtlasLod1Sectors.value = lod1Sectors;
        skyAtlasLod2Sectors.value = lod2Sectors;
        skyAtlasSectorMode.value = hasAny;
        skyAtlasMeta.value = {
            available: hasAny,
            version: Number(data?.version) || (lod2Sectors.length > 0 ? 3 : 2),
            mode: 'sectors',
            count: sourceCount,
            lod0Count: lod0Sectors.length,
            lod1Count: lod1Sectors.length,
            lod2Count: lod2Sectors.length,
            sheets: nextSheets.size,
            tileSize: lod0TileSize,
            generatedAt: genTag
        };
        skyAtlasReady.value = hasAny;
        skyAtlasVersion.value++;

        // Eagerly preload ONLY LOD 0 wide-sky sheet(s) (1 sheet = entire celestial sphere).
        // LOD 1 and LOD 2 detail sheets stream on demand when zoomed into their sky region.
        for (const [idx, meta] of nextSheets.entries()) {
            if (meta.lod === 0) {
                enqueueSheetDecode(idx, true);
            }
        }
        return;
    }

    // Legacy v1 per-target stamp fallback
    const nextByName = new Map();
    const nextBuckets = new Map();
    const preparedItems = [];
    const defaultTile = Number(data?.tile_size) || 128;
    const rawItems = Array.isArray(data?.items) ? data.items : [];

    for (let i = 0; i < rawItems.length; i++) {
        const it = rawItems[i];
        if (!it || it.ra === undefined || it.dec === undefined) continue;
        const ra = Number(it.ra);
        const dec = Number(it.dec);
        const fov = Math.max(Number(it.fov) || 1.0, 0.2);
        if (!Number.isFinite(ra) || !Number.isFinite(dec)) continue;

        const s = Number(it.s);
        if (!nextSheets.has(s)) continue;

        const a = ra * DEG2RAD;
        const d = dec * DEG2RAD;
        const cd = Math.cos(d);
        const norm = normalizeTargetKey(it.name);

        const record = {
            isSector: false,
            name: String(it.name || ''),
            normName: norm,
            commonName: String(it.common || ''),
            type: String(it.type || ''),
            ra,
            dec,
            x: cd * Math.cos(a),
            y: cd * Math.sin(a),
            z: Math.sin(d),
            fov,
            s,
            u: Number(it.u) || 0,
            v: Number(it.v) || 0,
            w: Number(it.w) || defaultTile,
            h: Number(it.h) || defaultTile,
            isCustom: Boolean(it.custom)
        };

        preparedItems.push(record);
        if (norm && !nextByName.has(norm)) {
            nextByName.set(norm, record);
        }
        const bKey = Math.floor(dec * 0.5);
        let bucket = nextBuckets.get(bKey);
        if (!bucket) {
            bucket = [];
            nextBuckets.set(bKey, bucket);
        }
        bucket.push(record);
    }

    itemByNormName = nextByName;
    decBuckets = nextBuckets;
    skyAtlasLod0Sectors.value = [];
    skyAtlasLod1Sectors.value = [];
    skyAtlasLod2Sectors.value = [];
    skyAtlasSectorMode.value = false;
    skyAtlasItems.value = preparedItems;
    skyAtlasMeta.value = {
        available: preparedItems.length > 0,
        version: 1,
        mode: 'items',
        count: preparedItems.length,
        sheets: nextSheets.size,
        tileSize: defaultTile,
        generatedAt: genTag
    };
    skyAtlasReady.value = preparedItems.length > 0;
    skyAtlasVersion.value++;

    for (const idx of nextSheets.keys()) {
        enqueueSheetDecode(idx, false);
    }
};

export const ensureSkyAtlasLoaded = (forceReload = false) => {
    if (loadPromise && !forceReload) {
        return loadPromise;
    }

    const cacheBust = forceReload ? `?t=${Date.now()}` : '';
    loadPromise = (async () => {
        try {
            const res = await fetch(`/sky_atlas/sky_atlas.json${cacheBust}`, {
                cache: forceReload ? 'no-cache' : 'default'
            });
            if (!res.ok) {
                if (forceReload) resetState();
                return false;
            }
            const data = await res.json();
            indexManifest(data);
            return skyAtlasReady.value;
        } catch {
            return false;
        }
    })();

    return loadPromise;
};

export const isAtlasSheetReady = (sheetIndex) => {
    const existing = sheetImageCache.get(sheetIndex);
    if (existing && existing.status === 'ready' && existing.img) {
        existing.lastTouch = ++sheetTouchClock;
        return true;
    }
    return false;
};

export const ensureAtlasSheetImage = (sheetIndex, requestLoad = true, highPriority = false) => {
    const existing = sheetImageCache.get(sheetIndex);
    if (existing) {
        if (existing.status === 'ready' && existing.img) {
            existing.lastTouch = ++sheetTouchClock;
            return existing.img;
        }
        return null;
    }
    if (requestLoad) {
        enqueueSheetDecode(sheetIndex, highPriority);
    }
    return null;
};

/**
 * Find the best matching pre-baked Sky Atlas tile for a target object or coordinate.
 */
export const findSkyAtlasTile = (target) => {
    if (!target || !skyAtlasReady.value) return null;

    if (skyAtlasSectorMode.value) {
        const ra = parseFloat(target.center_ra !== undefined ? target.center_ra : target.ra);
        const dec = parseFloat(target.center_dec !== undefined ? target.center_dec : target.dec);
        if (!Number.isFinite(ra) || !Number.isFinite(dec)) return null;
        const overlapping = findOverlappingAtlasTiles(ra, dec, 1.0, null);
        return overlapping.length > 0 ? overlapping[overlapping.length - 1] : null;
    }

    if (typeof target === 'string') {
        return itemByNormName.get(normalizeTargetKey(target)) || null;
    }

    const primaryNorm = normalizeTargetKey(target.name || target.id);
    if (primaryNorm) {
        const direct = itemByNormName.get(primaryNorm);
        if (direct) return direct;
    }

    if (typeof target.aliases === 'string' && target.aliases.trim()) {
        const parts = target.aliases.split(',');
        for (let i = 0; i < parts.length; i++) {
            const aliasNorm = normalizeTargetKey(parts[i]);
            if (aliasNorm) {
                const aliasHit = itemByNormName.get(aliasNorm);
                if (aliasHit) return aliasHit;
            }
        }
    }

    const ra = parseFloat(target.center_ra !== undefined ? target.center_ra : target.ra);
    const dec = parseFloat(target.center_dec !== undefined ? target.center_dec : target.dec);
    if (!Number.isFinite(ra) || !Number.isFinite(dec)) return null;

    const centerBucket = Math.floor(dec * 0.5);
    let bestTile = null;
    let bestDist = Infinity;

    for (let b = centerBucket - 2; b <= centerBucket + 2; b++) {
        const list = decBuckets.get(b);
        if (!list) continue;
        for (let i = 0; i < list.length; i++) {
            const cand = list[i];
            const maxReach = Math.max(0.05, cand.fov * 0.38);
            if (Math.abs(cand.dec - dec) > maxReach) continue;
            const dist = angularSeparationDeg(ra, dec, cand.ra, cand.dec);
            if (dist <= maxReach && dist < bestDist) {
                bestDist = dist;
                bestTile = cand;
            }
        }
    }

    return bestTile;
};

const collectOverlappingSectors = (sectors, fx, fy, fz, frameRad, cosF, sinF, requestLoad, highPriority) => {
    const allHits = [];
    const readyHits = [];
    let anyPending = false;

    const byPatch = sectors.byPatch;
    if (byPatch && sectors.length > 300) {
        const coneLimit = Math.min(Math.PI, frameRad + (sectors.maxRadiusRad || 0.14));
        const cosCone = Math.cos(coneLimit);
        const sinCone = Math.sin(coneLimit);
        for (let p = 0; p < PATCH_COUNT; p++) {
            const bucket = byPatch[p];
            if (bucket.length === 0) continue;
            if (coneLimit + patchRadius[p] < Math.PI) {
                const pThresh = cosCone * patchCosR[p] - sinCone * patchSinR[p];
                if (fx * patchCx[p] + fy * patchCy[p] + fz * patchCz[p] < pThresh) continue;
            }
            for (let i = 0; i < bucket.length; i++) {
                const sec = bucket[i];
                if (frameRad + sec.radiusRad < Math.PI) {
                    const thresh = cosF * sec.cosR - sinF * sec.sinR;
                    if (fx * sec.tx + fy * sec.ty + fz * sec.tz < thresh) continue;
                }
                allHits.push(sec);
                const ready = ensureAtlasSheetImage(sec.s, requestLoad, highPriority);
                if (ready) {
                    readyHits.push(sec);
                } else {
                    anyPending = true;
                }
            }
        }
        return { allHits, readyHits, anyPending };
    }

    for (let i = 0; i < sectors.length; i++) {
        const sec = sectors[i];
        if (frameRad + sec.radiusRad < Math.PI) {
            const thresh = cosF * sec.cosR - sinF * sec.sinR;
            if (fx * sec.tx + fy * sec.ty + fz * sec.tz < thresh) continue;
        }
        allHits.push(sec);
        const ready = ensureAtlasSheetImage(sec.s, requestLoad, highPriority);
        if (ready) {
            readyHits.push(sec);
        } else {
            anyPending = true;
        }
    }

    return { allHits, readyHits, anyPending };
};

/**
 * Return all pre-baked Sky Atlas sectors (or v1 tiles) overlapping a square framing window.
 * In multi-LOD sector mode, cascades LOD 2 -> LOD 1 -> LOD 0 so close-up Framing previews
 * get crisp ~155 px/deg LOD 2 imagery with instant progressive fallback while sheets decode.
 */
export const findOverlappingAtlasTiles = (ra, dec, frameFov, primaryTarget = null) => {
    if (!skyAtlasReady.value || !Number.isFinite(ra) || !Number.isFinite(dec)) return [];

    const fov = Math.max(Number(frameFov) || 1.0, 0.05);

    if (skyAtlasSectorMode.value) {
        const a = ra * DEG2RAD;
        const d = dec * DEG2RAD;
        const cd = Math.cos(d);
        const fx = cd * Math.cos(a);
        const fy = cd * Math.sin(a);
        const fz = Math.sin(d);
        const halfTanF = Math.tan(0.5 * Math.min(fov, 130.0) * DEG2RAD);
        const frameRad = Math.atan(Math.SQRT2 * halfTanF) + 0.02;
        const cosF = Math.cos(frameRad);
        const sinF = Math.sin(frameRad);

        const lod2 = skyAtlasLod2Sectors.value;
        const lod1 = skyAtlasLod1Sectors.value;
        const lod0 = skyAtlasLod0Sectors.value;

        // Close-up framing / zoom: prefer LOD 2 (~3 deg, ~155 px/deg)
        if (fov <= 22.0 && lod2.length > 0) {
            const l2 = collectOverlappingSectors(lod2, fx, fy, fz, frameRad, cosF, sinF, true, true);
            if (l2.allHits.length > 0 && !l2.anyPending) {
                return l2.readyHits;
            }
            // While LOD 2 sheets decode, composite already-ready LOD 1 sectors (without queueing unnecessary LOD 1 decodes if LOD 2 covers it)
            const requestLod1 = l2.allHits.length === 0;
            const l1 = lod1.length > 0
                ? collectOverlappingSectors(lod1, fx, fy, fz, frameRad, cosF, sinF, requestLod1, true)
                : { allHits: [], readyHits: [], anyPending: true };
            if (l1.allHits.length > 0 && !l1.anyPending) {
                return l1.readyHits.concat(l2.readyHits);
            }
            const l0 = collectOverlappingSectors(lod0, fx, fy, fz, frameRad, cosF, sinF, true, true);
            return l0.allHits.concat(l1.readyHits, l2.readyHits);
        }

        // Mid-field: prefer LOD 1 (~6 deg)
        if (fov <= 55.0 && lod1.length > 0) {
            const l1 = collectOverlappingSectors(lod1, fx, fy, fz, frameRad, cosF, sinF, true, true);
            if (l1.allHits.length > 0 && !l1.anyPending) {
                return l1.readyHits;
            }
            const l0 = collectOverlappingSectors(lod0, fx, fy, fz, frameRad, cosF, sinF, true, true);
            return l0.allHits.concat(l1.readyHits);
        }

        // Wide-field: LOD 0
        const l0 = collectOverlappingSectors(lod0, fx, fy, fz, frameRad, cosF, sinF, true, true);
        return l0.allHits;
    }

    // Legacy v1 fallback
    const primaryTile = primaryTarget ? findSkyAtlasTile(primaryTarget) : null;
    const searchRadius = Math.max(fov * 0.75 + 3.0, 4.0);
    const minBucket = Math.floor((dec - searchRadius) * 0.5);
    const maxBucket = Math.floor((dec + searchRadius) * 0.5);

    const hits = [];
    const seen = new Set();

    if (primaryTile) {
        seen.add(primaryTile);
        hits.push({
            tile: primaryTile,
            dist: angularSeparationDeg(ra, dec, primaryTile.ra, primaryTile.dec),
            isPrimary: true
        });
    }

    const cosDec = Math.max(Math.cos(dec * DEG2RAD), 0.01);

    for (let b = minBucket; b <= maxBucket; b++) {
        const list = decBuckets.get(b);
        if (!list) continue;
        for (let i = 0; i < list.length; i++) {
            const cand = list[i];
            if (seen.has(cand)) continue;
            const halfSpan = (fov + cand.fov) * 0.52;
            if (Math.abs(cand.dec - dec) > halfSpan) continue;

            let dRa = cand.ra - ra;
            if (dRa > 180) dRa -= 360;
            if (dRa < -180) dRa += 360;
            if (Math.abs(dRa * cosDec) > halfSpan) continue;

            seen.add(cand);
            hits.push({
                tile: cand,
                dist: Math.hypot(dRa * cosDec, cand.dec - dec),
                isPrimary: false
            });
        }
    }

    hits.sort((a, b) => {
        if (a.isPrimary !== b.isPrimary) return a.isPrimary ? 1 : -1;
        if (Math.abs(b.tile.fov - a.tile.fov) > 0.15) return b.tile.fov - a.tile.fov;
        return b.dist - a.dist;
    });

    return hits.map((h) => h.tile);
};

/**
 * Render an array of overlapping Sky Atlas sectors/tiles onto a square 2D canvas
 * using gnomonic tangent-plane projection (North up, East left).
 */
export const drawAtlasFrameToCanvas = (
    canvas,
    ra,
    dec,
    fov,
    tiles,
    {
        size = 512,
        requestLoad = true,
        highPriority = true,
        smoothingQuality = 'high'
    } = {}
) => {
    if (!canvas || !tiles || tiles.length === 0) return false;
    if (canvas.width !== size) canvas.width = size;
    if (canvas.height !== size) canvas.height = size;

    const ctx = canvas.getContext('2d', { alpha: false });
    if (!ctx) return false;

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = '#050912';
    ctx.fillRect(0, 0, size, size);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = smoothingQuality;

    const cosDec = Math.max(Math.cos(dec * DEG2RAD), 0.01);
    const frameBasis = makeTangentBasis(ra, dec);
    const halfTanFrame = Math.tan(0.5 * Math.min(fov, 130.0) * DEG2RAD);
    const invSpanScale = size / (2.0 * Math.max(halfTanFrame, 1e-5));

    const projectGnomonicX = (px, py, pz, invD) =>
        size * 0.5 -
        (px * frameBasis.ex + py * frameBasis.ey + pz * frameBasis.ez) * invD * invSpanScale;
    const projectGnomonicY = (px, py, pz, invD) =>
        size * 0.5 -
        (px * frameBasis.nx + py * frameBasis.ny + pz * frameBasis.nz) * invD * invSpanScale;

    let drewAny = false;

    for (let i = 0; i < tiles.length; i++) {
        const tile = tiles[i];
        const sheetImg = ensureAtlasSheetImage(tile.s, requestLoad, highPriority);
        if (!sheetImg) continue;

        if (tile.isSector) {
            const d0 = tile.tx * frameBasis.tx + tile.ty * frameBasis.ty + tile.tz * frameBasis.tz;
            const dL = tile.lx * frameBasis.tx + tile.ly * frameBasis.ty + tile.lz * frameBasis.tz;
            const dR = tile.rx * frameBasis.tx + tile.ry * frameBasis.ty + tile.rz * frameBasis.tz;
            const dT = tile.topX * frameBasis.tx + tile.topY * frameBasis.ty + tile.topZ * frameBasis.tz;
            const dB = tile.botX * frameBasis.tx + tile.botY * frameBasis.ty + tile.botZ * frameBasis.tz;
            if (d0 <= 0.05 || dL <= 0.05 || dR <= 0.05 || dT <= 0.05 || dB <= 0.05) continue;

            const inv0 = 1.0 / d0;
            const invL = 1.0 / dL;
            const invR = 1.0 / dR;
            const invT = 1.0 / dT;
            const invB = 1.0 / dB;

            const x0 = projectGnomonicX(tile.tx, tile.ty, tile.tz, inv0);
            const y0 = projectGnomonicY(tile.tx, tile.ty, tile.tz, inv0);
            const xL = projectGnomonicX(tile.lx, tile.ly, tile.lz, invL);
            const yL = projectGnomonicY(tile.lx, tile.ly, tile.lz, invL);
            const xR = projectGnomonicX(tile.rx, tile.ry, tile.rz, invR);
            const yR = projectGnomonicY(tile.rx, tile.ry, tile.rz, invR);
            const xT = projectGnomonicX(tile.topX, tile.topY, tile.topZ, invT);
            const yT = projectGnomonicY(tile.topX, tile.topY, tile.topZ, invT);
            const xB = projectGnomonicX(tile.botX, tile.botY, tile.botZ, invB);
            const yB = projectGnomonicY(tile.botX, tile.botY, tile.botZ, invB);

            const ux = xR - xL;
            const uy = yR - yL;
            const vx = xB - xT;
            const vy = yB - yT;
            const cx = x0 + 0.22 * (xL + xR + xT + xB - 4.0 * x0);
            const cy = y0 + 0.22 * (yL + yR + yT + yB - 4.0 * y0);

            ctx.setTransform(ux, uy, vx, vy, cx, cy);
            ctx.drawImage(sheetImg, tile.u, tile.v, tile.w, tile.h, -0.5, -0.5, 1.0, 1.0);
            drewAny = true;
            continue;
        }

        let dRa = tile.ra - ra;
        if (dRa > 180) dRa -= 360;
        if (dRa < -180) dRa += 360;

        const dxDeg = -dRa * cosDec;
        const dyDeg = -(tile.dec - dec);

        const destSize = (tile.fov / fov) * size;
        const destCx = size * 0.5 + (dxDeg / fov) * size;
        const destCy = size * 0.5 + (dyDeg / fov) * size;

        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.drawImage(
            sheetImg,
            tile.u + 0.5,
            tile.v + 0.5,
            tile.w - 1,
            tile.h - 1,
            destCx - destSize * 0.5,
            destCy - destSize * 0.5,
            destSize,
            destSize
        );
        drewAny = true;
    }

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    return drewAny;
};

// --- SHARED ZERO-JANK THUMBNAIL SNAPSHOT GENERATOR & LRU CACHE ---
let sharedThumbCanvas = null;
const THUMB_SIZE = 72;
const THUMB_CACHE_MAX = 650;
const thumbUrlCache = new Map();

export const clearAtlasThumbnailCache = () => {
    thumbUrlCache.clear();
};

const getSharedThumbCanvas = () => {
    if (!sharedThumbCanvas && typeof document !== 'undefined') {
        sharedThumbCanvas = document.createElement('canvas');
        sharedThumbCanvas.width = THUMB_SIZE;
        sharedThumbCanvas.height = THUMB_SIZE;
    }
    return sharedThumbCanvas;
};

/**
 * Generate or retrieve an on-the-fly FOV-cropped thumbnail snapshot (72x72 JPEG data URL)
 * from already-loaded Sky Atlas data without blocking scrolling or flooding sheet decodes.
 *
 * Returns `{ url: string, complete: boolean } | null`.
 * - Instant pass (`allowLoadDetail = false`): uses already-resident LOD 0 + any resident LOD 1/2 sheets (0 network requests).
 * - Idle settle pass (`allowLoadDetail = true`): if `fov < 3.2°`, queues low-priority LOD 1 sheet decode so narrow-FOV
 *   list thumbnails upgrade to crisp ~85 px/deg detail without touching the 73 close-up LOD 2 sheets.
 */
export const getAtlasThumbnailSnapshot = (
    targetKey,
    ra,
    dec,
    frameFov,
    { allowLoadDetail = false, primaryTarget = null } = {}
) => {
    if (!skyAtlasReady.value || !Number.isFinite(ra) || !Number.isFinite(dec)) return null;

    const fov = Math.max(Number(frameFov) || 1.0, 0.15);
    const fovBucket = fov.toFixed(2);
    const coordKey = `${targetKey || ''}|${ra.toFixed(3)}_${dec.toFixed(3)}|${fovBucket}`;

    const cached = thumbUrlCache.get(coordKey);
    if (cached && (cached.complete || !allowLoadDetail)) {
        // Refresh LRU position
        thumbUrlCache.delete(coordKey);
        thumbUrlCache.set(coordKey, cached);
        return cached;
    }

    const canvas = getSharedThumbCanvas();
    if (!canvas) return null;

    let tilesToDraw = [];
    let isComplete = true;

    if (skyAtlasSectorMode.value) {
        const a = ra * DEG2RAD;
        const d = dec * DEG2RAD;
        const cd = Math.cos(d);
        const fx = cd * Math.cos(a);
        const fy = cd * Math.sin(a);
        const fz = Math.sin(d);
        const halfTanF = Math.tan(0.5 * Math.min(fov, 130.0) * DEG2RAD);
        const frameRad = Math.atan(Math.SQRT2 * halfTanF) + 0.02;
        const cosF = Math.cos(frameRad);
        const sinF = Math.sin(frameRad);

        const lod0 = skyAtlasLod0Sectors.value;
        const lod1 = skyAtlasLod1Sectors.value;
        const lod2 = skyAtlasLod2Sectors.value;

        const l0 = collectOverlappingSectors(lod0, fx, fy, fz, frameRad, cosF, sinF, true, false);
        if (l0.allHits.length === 0 && lod1.length === 0) {
            return null;
        }

        if (fov >= 3.2 || lod1.length === 0) {
            tilesToDraw = l0.readyHits;
            isComplete = !l0.anyPending && l0.readyHits.length > 0;
        } else {
            // Check if any LOD 2 sectors happen to be resident in RAM already (free sharpness, never request load)
            const l2Resident =
                lod2.length > 0
                    ? collectOverlappingSectors(lod2, fx, fy, fz, frameRad, cosF, sinF, false, false)
                    : { allHits: [], readyHits: [], anyPending: false };

            if (l2Resident.allHits.length > 0 && !l2Resident.anyPending) {
                tilesToDraw = l2Resident.readyHits;
                isComplete = true;
            } else {
                const l1 = collectOverlappingSectors(
                    lod1,
                    fx,
                    fy,
                    fz,
                    frameRad,
                    cosF,
                    sinF,
                    allowLoadDetail,
                    false
                );
                if (l1.allHits.length > 0 && !l1.anyPending) {
                    tilesToDraw = l1.readyHits.concat(l2Resident.readyHits);
                    isComplete = true;
                } else {
                    tilesToDraw = l0.readyHits.concat(l1.readyHits, l2Resident.readyHits);
                    isComplete = l1.allHits.length === 0 && !l0.anyPending;
                }
            }
        }
    } else {
        tilesToDraw = findOverlappingAtlasTiles(ra, dec, fov, primaryTarget);
        isComplete = tilesToDraw.length > 0;
    }

    if (tilesToDraw.length === 0) {
        return cached || null;
    }

    // If we already cached an incomplete LOD 0 preview and detail sheets are still decoding, reuse it instead of re-encoding
    if (cached && !isComplete) {
        return cached;
    }

    const drew = drawAtlasFrameToCanvas(canvas, ra, dec, fov, tilesToDraw, {
        size: THUMB_SIZE,
        requestLoad: false,
        highPriority: false,
        smoothingQuality: 'medium'
    });
    if (!drew) return cached || null;

    const url = canvas.toDataURL('image/jpeg', 0.82);
    const entry = { url, complete: isComplete };

    if (thumbUrlCache.has(coordKey)) {
        thumbUrlCache.delete(coordKey);
    } else if (thumbUrlCache.size >= THUMB_CACHE_MAX) {
        const oldestKey = thumbUrlCache.keys().next().value;
        if (oldestKey !== undefined) {
            thumbUrlCache.delete(oldestKey);
        }
    }
    thumbUrlCache.set(coordKey, entry);
    return entry;
};
