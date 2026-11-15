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
export const skyAtlasTargets = shallowRef([]);
export const skyAtlasLod0Sectors = shallowRef([]);
export const skyAtlasLod1Sectors = shallowRef([]);
export const skyAtlasLod2Sectors = shallowRef([]);

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
    if (!Array.isArray(rawSectors)) return out;

    for (let i = 0; i < rawSectors.length; i++) {
        const sc = rawSectors[i];
        if (!sc || sc.ra === undefined || sc.dec === undefined) continue;
        const ra = Number(sc.ra);
        const dec = Number(sc.dec);
        const span = Number(sc.span);
        const s = Number(sc.s);
        if (!Number.isFinite(ra) || !Number.isFinite(dec) || !Number.isFinite(span) || span <= 0) {
            continue;
        }
        if (!validSheets.has(s)) continue;

        const halfTan = Math.tan(0.5 * span * DEG2RAD);
        const basis = makeTangentBasis(ra, dec);
        const invEdgeLen = 1.0 / Math.sqrt(1.0 + halfTan * halfTan);
        const radiusRad = Math.atan(Math.SQRT2 * halfTan);

        out.push({
            isSector: true,
            lod: lodLevel,
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
            u: Number(sc.u) || 0,
            v: Number(sc.v) || 0,
            w: Number(sc.w) || defaultTileSize,
            h: Number(sc.h) || defaultTileSize
        });
    }
    return out;
};

const prepareTargetsList = (rawTargets) => {
    const out = [];
    if (!Array.isArray(rawTargets)) return out;

    for (let i = 0; i < rawTargets.length; i++) {
        const row = rawTargets[i];
        if (!row) continue;
        if (Array.isArray(row)) {
            const name = String(row[0] || '');
            const commonName = String(row[1] || '');
            const ra = Number(row[2]);
            const dec = Number(row[3]);
            const fov = Math.max(Number(row[4]) || 1.0, 0.2);
            if (!name || !Number.isFinite(ra) || !Number.isFinite(dec)) continue;
            out.push({
                name,
                normName: normalizeTargetKey(name),
                commonName,
                ra,
                dec,
                fov,
                isCustom: Boolean(row[5])
            });
        } else if (typeof row === 'object') {
            const name = String(row.name || '');
            const commonName = String(row.common || row.commonName || '');
            const ra = Number(row.ra);
            const dec = Number(row.dec);
            const fov = Math.max(Number(row.fov) || 1.0, 0.2);
            if (!name || !Number.isFinite(ra) || !Number.isFinite(dec)) continue;
            out.push({
                name,
                normName: normalizeTargetKey(name),
                commonName,
                ra,
                dec,
                fov,
                isCustom: Boolean(row.custom || row.isCustom)
            });
        }
    }
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
    skyAtlasTargets.value = [];
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
        const parsedTargets = prepareTargetsList(data?.targets);

        const hasAny = lod0Sectors.length > 0 || lod1Sectors.length > 0 || lod2Sectors.length > 0;
        const sourceCount =
            Number(data?.source_count) ||
            parsedTargets.length ||
            lod2Sectors.length ||
            lod1Sectors.length ||
            lod0Sectors.length;

        itemByNormName = new Map();
        decBuckets = new Map();
        skyAtlasItems.value = [];
        skyAtlasTargets.value = parsedTargets;
        skyAtlasLod0Sectors.value = lod0Sectors;
        skyAtlasLod1Sectors.value = lod1Sectors;
        skyAtlasLod2Sectors.value = lod2Sectors;
        skyAtlasSectorMode.value = hasAny;
        skyAtlasMeta.value = {
            available: hasAny,
            version: Number(data?.version) || (lod2Sectors.length > 0 ? 3 : 2),
            mode: 'sectors',
            count: sourceCount,
            targetCount: parsedTargets.length,
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
    skyAtlasTargets.value = [];
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
