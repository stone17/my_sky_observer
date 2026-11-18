// Shared astronomical observing date, session bounds, twilight ephemeris,
// and real-time solar/lunar/target altitude helpers.

export const DEG2RAD = Math.PI / 180.0;
export const RAD2DEG = 180.0 / Math.PI;

export const wrapRaDeg = (ra) => {
    let r = Number(ra) % 360.0;
    if (r < 0) r += 360.0;
    return r;
};

const SHORT_WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const SHORT_MONTHS = [
    'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'
];

export const formatLocalClock = (ms) => {
    const dt = new Date(ms);
    const hh = String(dt.getHours()).padStart(2, '0');
    const mm = String(dt.getMinutes()).padStart(2, '0');
    return `${hh}:${mm}`;
};

export const formatDateInputYmd = (ms) => {
    const dt = new Date(ms);
    const y = dt.getFullYear();
    const m = String(dt.getMonth() + 1).padStart(2, '0');
    const d = String(dt.getDate()).padStart(2, '0');
    return `${y}-${m}-${d}`;
};

export const formatObsDateBadge = (ms, refNowMs = Date.now()) => {
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

export const formatShortEveningBadge = (evening, refNowMs = Date.now()) => {
    if (!evening || !Number.isFinite(evening.year)) return '';
    const ref = new Date(refNowMs);
    const mon = SHORT_MONTHS[evening.month0] || '';
    if (evening.year !== ref.getFullYear()) {
        return `${mon} ${evening.day} '${String(evening.year).slice(-2)}`;
    }
    return `${mon} ${evening.day}`;
};

export const getObservingEveningDate = (ms) => {
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

export const resolveObservingEvening = (isLiveTime, liveNowMs, effectiveTimeMs) => {
    if (isLiveTime) {
        const dt = new Date(liveNowMs);
        if (dt.getHours() >= 8) {
            const year = dt.getFullYear();
            const month0 = dt.getMonth();
            const day = dt.getDate();
            const key = `${year}-${String(month0 + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
            return { year, month0, day, key };
        }
    }
    return getObservingEveningDate(effectiveTimeMs);
};

export const resolveLiveTonightEveningKey = (liveNowMs = Date.now()) => {
    return resolveObservingEvening(true, liveNowMs, liveNowMs).key;
};

export const parseObserverLocation = (locOrSettings) => {
    const loc = locOrSettings?.location ? locOrSettings.location : locOrSettings;
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
};

export const computeLstDeg = (timeMs, lonDeg) => {
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

export const computeSolarEquatorial = (timeMs) => {
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

// Truncated Meeus lunar ephemeris + topocentric parallax (~0.3 deg accuracy) for instant 0ms Moon altitude preview
export const computeLunarAltitudeDeg = (timeMs, latDeg, lonDeg) => {
    const d = timeMs / 86400000.0 + 2440587.5 - 2451545.0;
    const L0 = wrapRaDeg(218.3164477 + 13.17639648 * d);
    const M = wrapRaDeg(357.5291092 + 0.98560028 * d) * DEG2RAD;
    const Mp = wrapRaDeg(134.9633964 + 13.06499295 * d) * DEG2RAD;
    const D = wrapRaDeg(297.8501921 + 12.19074912 * d) * DEG2RAD;
    const F = wrapRaDeg(93.272095 + 13.22935024 * d) * DEG2RAD;

    const lonEcl =
        (L0 +
            6.288774 * Math.sin(Mp) +
            1.274027 * Math.sin(2.0 * D - Mp) +
            0.658314 * Math.sin(2.0 * D) +
            0.213618 * Math.sin(2.0 * Mp) -
            0.185116 * Math.sin(M) -
            0.114332 * Math.sin(2.0 * F)) *
        DEG2RAD;

    const latEcl =
        (5.128122 * Math.sin(F) +
            0.280602 * Math.sin(Mp + F) +
            0.277693 * Math.sin(Mp - F) +
            0.173237 * Math.sin(2.0 * D - F)) *
        DEG2RAD;

    const rKm =
        385000.56 -
        20905.355 * Math.cos(Mp) -
        3699.111 * Math.cos(2.0 * D - Mp) -
        2955.968 * Math.cos(2.0 * D) -
        569.925 * Math.cos(2.0 * Mp);

    const eps = (23.439291 - 0.000013 * d) * DEG2RAD;
    const cosEps = Math.cos(eps);
    const sinEps = Math.sin(eps);
    const cosB = Math.cos(latEcl);
    const sinB = Math.sin(latEcl);
    const cosL = Math.cos(lonEcl);
    const sinL = Math.sin(lonEcl);

    const xEq = cosB * cosL;
    const yEq = cosEps * cosB * sinL - sinEps * sinB;
    const zEq = sinEps * cosB * sinL + cosEps * sinB;

    const raGeo = wrapRaDeg(Math.atan2(yEq, xEq) * RAD2DEG);
    const sinDec = Math.max(-1.0, Math.min(1.0, zEq));
    const cosDec = Math.sqrt(Math.max(0.0, 1.0 - sinDec * sinDec));

    const latRad = Math.max(-89.999, Math.min(89.999, latDeg)) * DEG2RAD;
    const haRad = (computeLstDeg(timeMs, lonDeg) - raGeo) * DEG2RAD;
    const sinAltGeo = Math.sin(latRad) * sinDec + Math.cos(latRad) * cosDec * Math.cos(haRad);
    const altGeoRad = Math.asin(Math.max(-1.0, Math.min(1.0, sinAltGeo)));

    // Topocentric altitude correction for lunar horizontal parallax
    const sinPi = Math.min(0.025, 6378.14 / Math.max(340000.0, rKm));
    const parallaxRad = Math.asin(sinPi * Math.cos(altGeoRad));
    return (altGeoRad - parallaxRad) * RAD2DEG;
};

const nightEphemerisCache = new Map();

export const computeNightEphemerisForEvening = (year, month0, day, latDeg, lonDeg) => {
    const cacheKey = `${year}-${month0}-${day}:${latDeg.toFixed(3)}:${lonDeg.toFixed(3)}`;
    const cached = nightEphemerisCache.get(cacheKey);
    if (cached) return cached;

    const clampedLat = Math.max(-89.8, Math.min(89.8, latDeg));
    const sinLat = Math.sin(clampedLat * DEG2RAD);
    const cosLat = Math.cos(clampedLat * DEG2RAD);

    // Refine true solar midnight (solar anti-transit, HA = 180 deg) for evening (year, month0, day)
    const approxMidMs = Date.UTC(year, month0, day, 24, 0, 0) - (lonDeg / 15.0) * 3600000.0;
    const sunMid = computeSolarEquatorial(approxMidMs);
    const lstMid = computeLstDeg(approxMidMs, lonDeg);
    let haErr = wrapRaDeg(lstMid - sunMid.raDeg - 180.0);
    if (haErr > 180.0) haErr -= 360.0;
    const solarMidMs = approxMidMs - (haErr / 360.0) * 86400000.0;

    const solveCrossingPair = (altDeg) => {
        const sinAlt = Math.sin(altDeg * DEG2RAD);
        const cosH0 = (sinAlt - sinLat * sunMid.sinDec) / Math.max(1e-6, cosLat * sunMid.cosDec);
        if (cosH0 < -1.0) return { state: 'never_below', eveMs: null, mornMs: null };
        if (cosH0 > 1.0) return { state: 'always_below', eveMs: null, mornMs: null };

        const halfNightDeg0 = 180.0 - Math.acos(cosH0) * RAD2DEG;
        const guessOffsetMs = (halfNightDeg0 / 360.0) * 86400000.0;

        const refineSide = (sign) => {
            const tGuess = solarMidMs + sign * guessOffsetMs;
            const sun = computeSolarEquatorial(tGuess);
            const cosH = (sinAlt - sinLat * sun.sinDec) / Math.max(1e-6, cosLat * sun.cosDec);
            if (cosH < -1.0 || cosH > 1.0) return tGuess;
            const halfNightDeg = 180.0 - Math.acos(cosH) * RAD2DEG;
            let dRa = sun.raDeg - sunMid.raDeg;
            if (dRa > 180.0) dRa -= 360.0;
            if (dRa < -180.0) dRa += 360.0;
            return solarMidMs + ((dRa + sign * halfNightDeg) / 360.0) * 86400000.0;
        };

        const eveMs = refineSide(-1.0);
        const mornMs = refineSide(1.0);
        if (!Number.isFinite(eveMs) || !Number.isFinite(mornMs) || mornMs <= eveMs) {
            return { state: 'invalid', eveMs: null, mornMs: null };
        }
        return { state: 'normal', eveMs, mornMs };
    };

    const horizon = solveCrossingPair(-0.833);
    const civil = solveCrossingPair(-6.0);
    const nautical = solveCrossingPair(-12.0);
    const astro = solveCrossingPair(-18.0);

    let startMs;
    let endMs;
    let midMs = solarMidMs;

    if (horizon.state === 'normal') {
        startMs = horizon.eveMs - 30 * 60 * 1000;
        endMs = horizon.mornMs + 30 * 60 * 1000;
        if (astro.state === 'normal') {
            midMs = 0.5 * (astro.eveMs + astro.mornMs);
        } else {
            midMs = 0.5 * (horizon.eveMs + horizon.mornMs);
        }
    } else {
        startMs = solarMidMs - 12 * 3600 * 1000;
        endMs = solarMidMs + 12 * 3600 * 1000;
    }

    const midSunAltDeg =
        Math.asin(Math.max(-1.0, Math.min(1.0, sinLat * sunMid.sinDec - cosLat * sunMid.cosDec))) *
        RAD2DEG;

    const ephem = {
        startMs,
        endMs,
        midMs,
        midSunAltDeg,
        sunsetMs: horizon.eveMs,
        sunriseMs: horizon.mornMs,
        civilEveMs: civil.eveMs,
        civilMornMs: civil.mornMs,
        nautEveMs: nautical.eveMs,
        nautMornMs: nautical.mornMs,
        astroEveMs: astro.eveMs,
        astroMornMs: astro.mornMs
    };

    if (nightEphemerisCache.size > 256) {
        nightEphemerisCache.clear();
    }
    nightEphemerisCache.set(cacheKey, ephem);
    return ephem;
};

export const buildNightTimesFromEphemeris = (ephem) => {
    if (!ephem) return {};
    const iso = (ms) => (Number.isFinite(ms) ? new Date(ms).toISOString() : null);
    const nt = {};

    if (Number.isFinite(ephem.sunsetMs) && Number.isFinite(ephem.sunriseMs)) {
        const s0 = ephem.sunsetMs;
        const s1 = ephem.sunriseMs;
        const c0 = ephem.civilEveMs;
        const c1 = ephem.civilMornMs;
        const n0 = ephem.nautEveMs;
        const n1 = ephem.nautMornMs;
        const a0 = ephem.astroEveMs;
        const a1 = ephem.astroMornMs;

        if (Number.isFinite(c0) && Number.isFinite(c1) && c1 > c0) {
            nt.civil = [iso(s0), iso(c0)];
            nt.civil_morn = [iso(c1), iso(s1)];

            if (Number.isFinite(n0) && Number.isFinite(n1) && n1 > n0) {
                nt.nautical = [iso(c0), iso(n0)];
                nt.nautical_morn = [iso(n1), iso(c1)];

                if (Number.isFinite(a0) && Number.isFinite(a1) && a1 > a0) {
                    nt.astronomical = [iso(n0), iso(a0)];
                    nt.night = [iso(a0), iso(a1)];
                    nt.astronomical_morn = [iso(a1), iso(n1)];
                } else {
                    nt.astronomical = [iso(n0), iso(n1)];
                }
            } else {
                nt.nautical = [iso(c0), iso(c1)];
            }
        } else {
            nt.civil = [iso(s0), iso(s1)];
        }
    } else if (ephem.midSunAltDeg <= -18.0) {
        nt.night = [iso(ephem.startMs), iso(ephem.endMs)];
    } else if (ephem.midSunAltDeg <= -12.0) {
        nt.astronomical = [iso(ephem.startMs), iso(ephem.endMs)];
    } else if (ephem.midSunAltDeg <= -6.0) {
        nt.nautical = [iso(ephem.startMs), iso(ephem.endMs)];
    } else if (ephem.midSunAltDeg < -0.833) {
        nt.civil = [iso(ephem.startMs), iso(ephem.endMs)];
    }

    return nt;
};

export const doesGraphMatchEvening = (graph, eveningKey) => {
    if (!Array.isArray(graph) || graph.length < 2 || !eveningKey) return false;
    const t0 = Date.parse(graph[0]?.time);
    const t1 = Date.parse(graph[graph.length - 1]?.time);
    if (!Number.isFinite(t0) || !Number.isFinite(t1) || t1 <= t0) return false;
    const spanHours = (t1 - t0) / 3600000.0;
    if (spanHours > 25.5) return false;
    const midMs = 0.5 * (t0 + t1);
    return getObservingEveningDate(midMs).key === eveningKey;
};

export const doesNightTimesMatchEvening = (nightTimes, eveningKey) => {
    if (!nightTimes || typeof nightTimes !== 'object' || !eveningKey) return false;
    const refRange =
        nightTimes.night ||
        nightTimes.astronomical ||
        nightTimes.nautical ||
        nightTimes.civil;
    if (!Array.isArray(refRange) || refRange.length !== 2) return false;
    const t0 = Date.parse(refRange[0]);
    let t1 = Date.parse(refRange[1]);
    const mornPair =
        nightTimes.civil_morn ||
        nightTimes.nautical_morn ||
        nightTimes.astronomical_morn;
    if (Array.isArray(mornPair) && mornPair.length === 2) {
        const tm1 = Date.parse(mornPair[1]);
        if (Number.isFinite(tm1) && tm1 > t1) t1 = tm1;
    }
    if (!Number.isFinite(t0) || !Number.isFinite(t1) || t1 < t0) return false;
    const midMs = 0.5 * (t0 + t1);
    return getObservingEveningDate(midMs).key === eveningKey;
};

export const resolveSessionRangeForEvening = ({
    evening,
    observerLocation,
    object,
    nightTimes
}) => {
    const eve = evening || resolveObservingEvening(true, Date.now(), Date.now());

    // 1. Prefer backend altitude_graph session bounds if they belong to this exact evening
    const graph = object?.altitude_graph;
    if (doesGraphMatchEvening(graph, eve.key)) {
        const s = Date.parse(graph[0].time);
        const e = Date.parse(graph[graph.length - 1].time);
        if (Number.isFinite(s) && Number.isFinite(e) && e > s) {
            let midMs = (s + e) * 0.5;
            if (
                doesNightTimesMatchEvening(nightTimes, eve.key) &&
                Array.isArray(nightTimes?.night) &&
                nightTimes.night.length === 2
            ) {
                const ns = Date.parse(nightTimes.night[0]);
                const ne = Date.parse(nightTimes.night[1]);
                if (Number.isFinite(ns) && Number.isFinite(ne) && ne > ns) {
                    midMs = (ns + ne) * 0.5;
                }
            }
            return { startMs: s, endMs: e, midMs };
        }
    }

    // 2. Fallback to nightTimes if they match this exact evening
    if (doesNightTimesMatchEvening(nightTimes, eve.key)) {
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
            const pair = nightTimes[key];
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

    // 3. Computed solar twilight bounds for any selected date & observer location
    if (observerLocation && Number.isFinite(observerLocation.lat) && Number.isFinite(observerLocation.lon)) {
        const ephem = computeNightEphemerisForEvening(
            eve.year,
            eve.month0,
            eve.day,
            observerLocation.lat,
            observerLocation.lon
        );
        if (ephem) {
            return {
                startMs: ephem.startMs,
                endMs: ephem.endMs,
                midMs: ephem.midMs
            };
        }
    }

    // 4. Clean local-clock fallback (17:00 -> 07:00 on observing evening)
    const startMs = new Date(eve.year, eve.month0, eve.day, 17, 0, 0, 0).getTime();
    const endMs = startMs + 14 * 3600 * 1000;
    return { startMs, endMs, midMs: (startMs + endMs) * 0.5 };
};

export const buildTwilightSliderStyle = ({
    startMs,
    endMs,
    evening,
    nightTimes,
    ephem
}) => {
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

    let s0 = null;
    let s1 = null;
    let c0 = null;
    let c1 = null;
    let n0 = null;
    let n1 = null;
    let a0 = null;
    let a1 = null;

    const nt =
        evening && doesNightTimesMatchEvening(nightTimes, evening.key) ? nightTimes : null;

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
};
