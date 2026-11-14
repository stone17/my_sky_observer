import { ref, shallowRef } from 'vue';

const DEG2RAD = Math.PI / 180.0;
const RAD2DEG = 180.0 / Math.PI;

export const skyAtlasVersion = ref(0);
export const skyAtlasReady = ref(false);
export const skyAtlasMeta = shallowRef({
    available: false,
    count: 0,
    sheets: 0,
    tileSize: 128,
    generatedAt: ''
});
export const skyAtlasItems = shallowRef([]);

let loadPromise = null;
let sheetMetaByIndex = new Map();
let sheetImageCache = new Map();
let itemByNormName = new Map();
let decBuckets = new Map(); // integer floor(dec / 2) -> item[]

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
            file: String(sh.file),
            url: `/sky_atlas/${sh.file}${query}`,
            width: Number(sh.width) || 4096,
            height: Number(sh.height) || 4096,
            tiles: Number(sh.tiles) || 0
        });
    }

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

        const record = {
            name: String(it.name || ''),
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

        const norm = normalizeTargetKey(record.name);
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

    sheetMetaByIndex = nextSheets;
    sheetImageCache = new Map();
    itemByNormName = nextByName;
    decBuckets = nextBuckets;
    skyAtlasItems.value = preparedItems;
    skyAtlasMeta.value = {
        available: preparedItems.length > 0,
        count: preparedItems.length,
        sheets: nextSheets.size,
        tileSize: defaultTile,
        generatedAt: genTag
    };
    skyAtlasReady.value = preparedItems.length > 0;
    skyAtlasVersion.value++;
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
                if (forceReload) {
                    sheetMetaByIndex = new Map();
                    sheetImageCache = new Map();
                    itemByNormName = new Map();
                    decBuckets = new Map();
                    skyAtlasItems.value = [];
                    skyAtlasMeta.value = {
                        available: false,
                        count: 0,
                        sheets: 0,
                        tileSize: 128,
                        generatedAt: ''
                    };
                    skyAtlasReady.value = false;
                    skyAtlasVersion.value++;
                }
                return false;
            }
            const data = await res.json();
            indexManifest(data);
            return true;
        } catch {
            return false;
        }
    })();

    return loadPromise;
};

export const ensureAtlasSheetImage = (sheetIndex) => {
    const meta = sheetMetaByIndex.get(sheetIndex);
    if (!meta) return null;

    const existing = sheetImageCache.get(sheetIndex);
    if (existing) {
        return existing.status === 'ready' ? existing.img : null;
    }

    const img = new Image();
    img.decoding = 'async';
    const record = { img, status: 'loading' };
    sheetImageCache.set(sheetIndex, record);

    img.onload = () => {
        record.status = 'ready';
        skyAtlasVersion.value++;
    };
    img.onerror = () => {
        record.status = 'error';
    };
    img.src = meta.url;
    return null;
};

/**
 * Find the best matching pre-baked Sky Atlas tile for a target object or coordinate.
 */
export const findSkyAtlasTile = (target) => {
    if (!target || !skyAtlasReady.value) return null;

    if (typeof target === 'string') {
        return itemByNormName.get(normalizeTargetKey(target)) || null;
    }

    // 1. Direct normalized primary ID match
    const primaryNorm = normalizeTargetKey(target.name || target.id);
    if (primaryNorm) {
        const direct = itemByNormName.get(primaryNorm);
        if (direct) return direct;
    }

    // 2. Cross-catalog alias match (e.g. "M 42" <-> "NGC 1976")
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

    // 3. Spatial coordinate lookup (covers same-coord aliases or close neighbor inside wide tile)
    const ra = parseFloat(target.center_ra !== undefined ? target.center_ra : target.ra);
    const dec = parseFloat(target.center_dec !== undefined ? target.center_dec : target.dec);
    if (!Number.isFinite(ra) || !Number.isFinite(dec)) return null;

    const centerBucket = Math.floor(dec * 0.5);
    let bestTile = null;
    let bestDist = Infinity;

    // Scan +-2 buckets (+-4 deg declination) to support wide-field neighbor coverage
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

/**
 * Return all pre-baked Sky Atlas tiles overlapping the given square framing window,
 * ordered back-to-front (widest/farthest background first, primary/closest tile on top).
 */
export const findOverlappingAtlasTiles = (ra, dec, frameFov, primaryTarget = null) => {
    if (!skyAtlasReady.value || !Number.isFinite(ra) || !Number.isFinite(dec)) return [];

    const fov = Math.max(Number(frameFov) || 1.0, 0.05);
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

    // Back-to-front: draw background/wider tiles first, primary/closest/higher-res tile last
    hits.sort((a, b) => {
        if (a.isPrimary !== b.isPrimary) return a.isPrimary ? 1 : -1;
        if (Math.abs(b.tile.fov - a.tile.fov) > 0.15) return b.tile.fov - a.tile.fov;
        return b.dist - a.dist;
    });

    return hits.map((h) => h.tile);
};

