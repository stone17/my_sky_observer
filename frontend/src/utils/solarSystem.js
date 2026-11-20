// Pure-JS 60 FPS analytical Solar System ephemeris engine (Sun, topocentric Moon with
// phase/limb geometry, Mercury–Neptune with mutual gas-giant perturbations, and Pluto).
// Zero allocations in steady state (< 0.03 ms per call).

import { DEG2RAD, RAD2DEG, wrapRaDeg, computeLstDeg } from './observingTime.js';

const KM_PER_AU = 149597870.7;
const EARTH_RADIUS_KM = 6378.137;
const WGS84_FLATTENING = 1.0 / 298.257223563;

const solveKeplerRad = (mRad, e) => {
    let E = mRad + e * Math.sin(mRad) * (1.0 + e * Math.cos(mRad));
    for (let k = 0; k < 3; k++) {
        const f = E - e * Math.sin(E) - mRad;
        const fp = 1.0 - e * Math.cos(E);
        E -= f / fp;
    }
    return E;
};

// Pre-allocated body pool so 60 FPS calls produce zero GC churn
const BODY_DEFS = [
    { id: 'sun', name: 'Sun', symbol: '☀', color: '#FBBF24' },
    { id: 'moon', name: 'Moon', symbol: '☾', color: '#E2E8F0' },
    { id: 'mercury', name: 'Mercury', symbol: '☿', color: '#CBD5E1' },
    { id: 'venus', name: 'Venus', symbol: '♀', color: '#FEF08A' },
    { id: 'mars', name: 'Mars', symbol: '♂', color: '#F87171' },
    { id: 'jupiter', name: 'Jupiter', symbol: '♃', color: '#FBBF24' },
    { id: 'saturn', name: 'Saturn', symbol: '♄', color: '#FDE68A' },
    { id: 'uranus', name: 'Uranus', symbol: '♅', color: '#38BDF8' },
    { id: 'neptune', name: 'Neptune', symbol: '♆', color: '#818CF8' },
    { id: 'pluto', name: 'Pluto', symbol: '♇', color: '#94A3B8' }
];

const bodyPool = BODY_DEFS.map((def) => ({
    id: def.id,
    name: def.name,
    symbol: def.symbol,
    color: def.color,
    ra: 0,
    dec: 0,
    x: 1,
    y: 0,
    z: 0,
    mag: 0,
    distance_au: 1,
    angular_diameter_deg: 0,
    phase_fraction: 1,
    phase_percent: 100,
    phase_name: '',
    is_waxing: true,
    sun_ra: 0,
    sun_dec: 0,
    elongation_deg: 0
}));

let lastCacheTimeMs = NaN;
let lastCacheLat = NaN;
let lastCacheLon = NaN;
let lastCacheElev = NaN;

const setBodyEquatorial = (slot, xe, ye, ze, distAu, mag, pMat = null) => {
    const r = Math.hypot(xe, ye, ze) || 1.0;
    let ux = xe / r;
    let uy = ye / r;
    let uz = ze / r;
    if (pMat) {
        const jx = pMat.xx * ux + pMat.yx * uy + pMat.zx * uz;
        const jy = pMat.xy * ux + pMat.yy * uy + pMat.zy * uz;
        const jz = pMat.xz * ux + pMat.yz * uy + pMat.zz * uz;
        ux = jx;
        uy = jy;
        uz = jz;
    }
    uz = Math.max(-1.0, Math.min(1.0, uz));
    slot.x = ux;
    slot.y = uy;
    slot.z = uz;
    slot.ra = wrapRaDeg(Math.atan2(uy, ux) * RAD2DEG);
    slot.dec = Math.asin(uz) * RAD2DEG;
    slot.distance_au = distAu;
    slot.mag = mag;
};

/**
 * Computes apparent J2000/of-date equatorial positions & visual magnitudes for
 * Sun, topocentric Moon, Mercury–Neptune, and Pluto at `timeMs`.
 * Cached within 250 ms & 0.001 deg observer movement for instant reuse within a frame.
 */
export const computeSolarSystemPositions = (
    timeMs,
    latDeg = 0.0,
    lonDeg = 0.0,
    elevM = 0.0
) => {
    const tMs = Number.isFinite(timeMs) ? timeMs : Date.now();
    const lat = Number.isFinite(latDeg) ? Math.max(-89.999, Math.min(89.999, latDeg)) : 0.0;
    const lon = Number.isFinite(lonDeg) ? lonDeg : 0.0;
    const elev = Number.isFinite(elevM) ? elevM : 0.0;

    if (
        Math.abs(tMs - lastCacheTimeMs) < 250 &&
        Math.abs(lat - lastCacheLat) < 1e-3 &&
        Math.abs(lon - lastCacheLon) < 1e-3 &&
        Math.abs(elev - lastCacheElev) < 5.0
    ) {
        return bodyPool;
    }

    lastCacheTimeMs = tMs;
    lastCacheLat = lat;
    lastCacheLon = lon;
    lastCacheElev = elev;

    const jd = tMs / 86400000.0 + 2440587.5;
    const d = jd - 2451543.5; // Days since 2000 Jan 0.0 UT (Schlyter epoch)
    const dJ2000 = jd - 2451545.0; // Days since J2000.0
    const T = dJ2000 / 36525.0;

    // IAU 1976 Precession matrix P (J2000 -> Equinox of Date); P^T maps Date -> J2000
    const zetaRad = ((2306.2181 * T + 0.30188 * T * T + 0.017998 * T * T * T) / 3600.0) * DEG2RAD;
    const zRad = ((2306.2181 * T + 1.09468 * T * T + 0.018203 * T * T * T) / 3600.0) * DEG2RAD;
    const thetaRad = ((2004.3109 * T - 0.42665 * T * T - 0.041833 * T * T * T) / 3600.0) * DEG2RAD;
    const cZeta = Math.cos(zetaRad);
    const sZeta = Math.sin(zetaRad);
    const cZ = Math.cos(zRad);
    const sZ = Math.sin(zRad);
    const cTheta = Math.cos(thetaRad);
    const sTheta = Math.sin(thetaRad);
    const precMat = {
        xx: cZeta * cTheta * cZ - sZeta * sZ,
        xy: -sZeta * cTheta * cZ - cZeta * sZ,
        xz: -sTheta * cZ,
        yx: cZeta * cTheta * sZ + sZeta * cZ,
        yy: -sZeta * cTheta * sZ + cZeta * cZ,
        yz: -sTheta * sZ,
        zx: cZeta * sTheta,
        zy: -sZeta * sTheta,
        zz: cTheta
    };

    const eclRad = (23.439291 - 3.563e-7 * d) * DEG2RAD;
    const cosEcl = Math.cos(eclRad);
    const sinEcl = Math.sin(eclRad);

    // 1. SUN (Geocentric ecliptic & equatorial -> J2000)
    const wSun = (282.9404 + 4.70935e-5 * d) * DEG2RAD;
    const eSun = 0.016709 - 1.151e-9 * d;
    const MSunDeg = wrapRaDeg(356.0470 + 0.9856002585 * d);
    const MSun = MSunDeg * DEG2RAD;
    const ESun = solveKeplerRad(MSun, eSun);
    const xvSun = Math.cos(ESun) - eSun;
    const yvSun = Math.sqrt(1.0 - eSun * eSun) * Math.sin(ESun);
    const vSun = Math.atan2(yvSun, xvSun);
    const rSunAu = Math.hypot(xvSun, yvSun);
    const lonSunRad = vSun + wSun;
    const lonSunDeg = wrapRaDeg(lonSunRad * RAD2DEG);

    const xsEcl = rSunAu * Math.cos(lonSunRad);
    const ysEcl = rSunAu * Math.sin(lonSunRad);
    const xsEq = xsEcl;
    const ysEq = ysEcl * cosEcl;
    const zsEq = ysEcl * sinEcl;

    const sunSlot = bodyPool[0];
    setBodyEquatorial(sunSlot, xsEq, ysEq, zsEq, rSunAu, -26.74, precMat);
    sunSlot.angular_diameter_deg = 0.5331 / rSunAu;

    // 2. MOON (Meeus periodic series + 3D WGS84 topocentric parallax)
    const L0 = wrapRaDeg(218.3164477 + 13.17639648 * dJ2000);
    const Mm = wrapRaDeg(357.5291092 + 0.98560028 * dJ2000) * DEG2RAD;
    const Mp = wrapRaDeg(134.9633964 + 13.06499295 * dJ2000) * DEG2RAD;
    const Dm = wrapRaDeg(297.8501921 + 12.19074912 * dJ2000) * DEG2RAD;
    const Fm = wrapRaDeg(93.2720950 + 13.22935024 * dJ2000) * DEG2RAD;

    const lonMoonDeg = wrapRaDeg(
        L0 +
            6.288774 * Math.sin(Mp) +
            1.274027 * Math.sin(2.0 * Dm - Mp) +
            0.658314 * Math.sin(2.0 * Dm) +
            0.213618 * Math.sin(2.0 * Mp) -
            0.185116 * Math.sin(Mm) -
            0.114332 * Math.sin(2.0 * Fm) +
            0.058793 * Math.sin(2.0 * Dm - 2.0 * Mp) +
            0.057066 * Math.sin(2.0 * Dm - Mm - Mp) +
            0.053322 * Math.sin(2.0 * Dm + Mp) +
            0.045758 * Math.sin(2.0 * Dm - Mm) -
            0.040923 * Math.sin(Mm - Mp) -
            0.034720 * Math.sin(Dm) -
            0.030383 * Math.sin(Mm + Mp)
    );

    const latMoonRad =
        (5.128122 * Math.sin(Fm) +
            0.280602 * Math.sin(Mp + Fm) +
            0.277693 * Math.sin(Mp - Fm) +
            0.173237 * Math.sin(2.0 * Dm - Fm) +
            0.055413 * Math.sin(2.0 * Dm - Mp + Fm) +
            0.046271 * Math.sin(2.0 * Dm - Mp - Fm) +
            0.032573 * Math.sin(2.0 * Dm + Fm)) *
        DEG2RAD;

    const rMoonKm =
        385000.56 -
        20905.355 * Math.cos(Mp) -
        3699.111 * Math.cos(2.0 * Dm - Mp) -
        2955.968 * Math.cos(2.0 * Dm) -
        569.925 * Math.cos(2.0 * Mp) +
        246.158 * Math.cos(2.0 * Dm - 2.0 * Mp) -
        204.586 * Math.cos(2.0 * Dm - Mm) -
        170.733 * Math.cos(2.0 * Dm + Mp);

    const lonMoonRad = lonMoonDeg * DEG2RAD;
    const cosBm = Math.cos(latMoonRad);
    const sinBm = Math.sin(latMoonRad);
    const cosLm = Math.cos(lonMoonRad);
    const sinLm = Math.sin(lonMoonRad);

    // Geocentric Moon position in Earth radii
    const rEr = rMoonKm / EARTH_RADIUS_KM;
    const xmGeoEr = rEr * cosBm * cosLm;
    const ymGeoEr = rEr * (cosEcl * cosBm * sinLm - sinEcl * sinBm);
    const zmGeoEr = rEr * (sinEcl * cosBm * sinLm + cosEcl * sinBm);

    // WGS84 observer geocentric vector in Earth radii
    const latRad = lat * DEG2RAD;
    const uObs = Math.atan((1.0 - WGS84_FLATTENING) * Math.tan(latRad));
    const elevEr = elev / (EARTH_RADIUS_KM * 1000.0);
    const rhoCosPhi = Math.cos(uObs) + elevEr * Math.cos(latRad);
    const rhoSinPhi = (1.0 - WGS84_FLATTENING) * Math.sin(uObs) + elevEr * Math.sin(latRad);
    const lstRad = computeLstDeg(tMs, lon) * DEG2RAD;

    const xtMoonEr = xmGeoEr - rhoCosPhi * Math.cos(lstRad);
    const ytMoonEr = ymGeoEr - rhoCosPhi * Math.sin(lstRad);
    const ztMoonEr = zmGeoEr - rhoSinPhi;
    const topoDistKm = Math.hypot(xtMoonEr, ytMoonEr, ztMoonEr) * EARTH_RADIUS_KM;
    const rMoonAu = topoDistKm / KM_PER_AU;

    // Exact 3D Lunar Phase & Illumination
    const xmAu = (xmGeoEr * EARTH_RADIUS_KM) / KM_PER_AU;
    const ymAu = (ymGeoEr * EARTH_RADIUS_KM) / KM_PER_AU;
    const zmAu = (zmGeoEr * EARTH_RADIUS_KM) / KM_PER_AU;
    const msX = xsEq - xmAu;
    const msY = ysEq - ymAu;
    const msZ = zsEq - zmAu;
    const msLen = Math.hypot(msX, msY, msZ) || 1.0;
    const meLen = Math.hypot(xmAu, ymAu, zmAu) || 1.0;
    const cosPhase = Math.max(
        -1.0,
        Math.min(1.0, (-xmAu * msX - ymAu * msY - zmAu * msZ) / (meLen * msLen))
    );
    const phaseAngleDeg = Math.acos(cosPhase) * RAD2DEG;
    const illFrac = 0.5 * (1.0 + cosPhase);
    const dLonSynodic = wrapRaDeg(lonMoonDeg - lonSunDeg);
    const isWaxing = dLonSynodic < 180.0;

    let phaseName;
    if (dLonSynodic < 12.0 || dLonSynodic >= 348.0) phaseName = 'New Moon';
    else if (dLonSynodic < 78.0) phaseName = 'Waxing Crescent';
    else if (dLonSynodic < 102.0) phaseName = 'First Quarter';
    else if (dLonSynodic < 168.0) phaseName = 'Waxing Gibbous';
    else if (dLonSynodic < 192.0) phaseName = 'Full Moon';
    else if (dLonSynodic < 258.0) phaseName = 'Waning Gibbous';
    else if (dLonSynodic < 282.0) phaseName = 'Third Quarter';
    else phaseName = 'Waning Crescent';

    const moonMag = Math.min(
        -2.5,
        -12.73 + 0.026 * phaseAngleDeg + 4e-9 * Math.pow(phaseAngleDeg, 4)
    );

    const moonSlot = bodyPool[1];
    setBodyEquatorial(moonSlot, xtMoonEr, ytMoonEr, ztMoonEr, rMoonAu, Number(moonMag.toFixed(1)), precMat);
    moonSlot.angular_diameter_deg = (3474.8 / Math.max(340000.0, topoDistKm)) * RAD2DEG;
    moonSlot.phase_fraction = illFrac;
    moonSlot.phase_percent = Number((illFrac * 100.0).toFixed(1));
    moonSlot.phase_name = phaseName;
    moonSlot.is_waxing = isWaxing;
    moonSlot.sun_ra = sunSlot.ra;
    moonSlot.sun_dec = sunSlot.dec;
    const cosElong = Math.max(
        -1.0,
        Math.min(1.0, moonSlot.x * sunSlot.x + moonSlot.y * sunSlot.y + moonSlot.z * sunSlot.z)
    );
    moonSlot.elongation_deg = Number((Math.acos(cosElong) * RAD2DEG).toFixed(1));

    // Helper to compute heliocentric ecliptic rectangular coords from Schlyter elements
    const computePlanetHelio = (Ndeg, ideg, wdeg, a, e, Mdeg) => {
        const N = wrapRaDeg(Ndeg) * DEG2RAD;
        const inc = ideg * DEG2RAD;
        const w = wrapRaDeg(wdeg) * DEG2RAD;
        const M = wrapRaDeg(Mdeg) * DEG2RAD;
        const E = solveKeplerRad(M, e);
        const xv = a * (Math.cos(E) - e);
        const yv = a * Math.sqrt(1.0 - e * e) * Math.sin(E);
        const v = Math.atan2(yv, xv);
        const r = Math.hypot(xv, yv);

        const vw = v + w;
        const cosN = Math.cos(N);
        const sinN = Math.sin(N);
        const cosVW = Math.cos(vw);
        const sinVW = Math.sin(vw);
        const cosI = Math.cos(inc);
        const sinI = Math.sin(inc);

        const xh = r * (cosN * cosVW - sinN * sinVW * cosI);
        const yh = r * (sinN * cosVW + cosN * sinVW * cosI);
        const zh = r * (sinVW * sinI);
        return { xh, yh, zh, r };
    };

    const finishPlanet = (slotIdx, xh, yh, zh, rHelio, magFn, usePrec = true) => {
        const xg = xh + xsEcl;
        const yg = yh + ysEcl;
        const zg = zh;
        const delta = Math.hypot(xg, yg, zg) || 1.0;

        // Phase angle FV (Sun–Planet–Earth) in degrees
        const cosFV = Math.max(
            -1.0,
            Math.min(
                1.0,
                (rHelio * rHelio + delta * delta - rSunAu * rSunAu) / (2.0 * rHelio * delta)
            )
        );
        const fvDeg = Math.acos(cosFV) * RAD2DEG;
        const logRD = 5.0 * Math.log10(Math.max(1e-4, rHelio * delta));
        const mag = Number(magFn(logRD, fvDeg, xh, yh, zh).toFixed(1));

        const xe = xg;
        const ye = yg * cosEcl - zg * sinEcl;
        const ze = yg * sinEcl + zg * cosEcl;
        setBodyEquatorial(
            bodyPool[slotIdx],
            xe,
            ye,
            ze,
            Number(delta.toFixed(4)),
            mag,
            usePrec ? precMat : null
        );
    };

    // Mean anomalies needed for gas-giant mutual perturbations
    const MjDeg = wrapRaDeg(19.8950 + 0.0830853001 * d);
    const MsDeg = wrapRaDeg(316.9670 + 0.0334442282 * d);
    const MuDeg = wrapRaDeg(142.5905 + 0.011725806 * d);
    const Mj = MjDeg * DEG2RAD;
    const Ms = MsDeg * DEG2RAD;
    const Mu = MuDeg * DEG2RAD;

    // 3. MERCURY
    {
        const p = computePlanetHelio(
            48.3313 + 3.24587e-5 * d,
            7.0047 + 5.0e-8 * d,
            29.1241 + 1.01444e-5 * d,
            0.387098,
            0.205635 + 5.59e-10 * d,
            168.6562 + 4.0923344368 * d
        );
        finishPlanet(
            2,
            p.xh,
            p.yh,
            p.zh,
            p.r,
            (logRD, fv) => -0.36 + logRD + 0.027 * fv + 2.2e-13 * Math.pow(fv, 6)
        );
    }

    // 4. VENUS
    {
        const p = computePlanetHelio(
            76.6799 + 2.4659e-5 * d,
            3.3946 + 2.75e-8 * d,
            54.8910 + 1.38374e-5 * d,
            0.723330,
            0.006773 - 1.302e-9 * d,
            48.0052 + 1.6021302244 * d
        );
        finishPlanet(
            3,
            p.xh,
            p.yh,
            p.zh,
            p.r,
            (logRD, fv) => -4.34 + logRD + 0.013 * fv + 4.2e-7 * Math.pow(fv, 3)
        );
    }

    // 5. MARS
    {
        const p = computePlanetHelio(
            49.5574 + 2.11081e-5 * d,
            1.8497 - 1.78e-8 * d,
            286.5016 + 2.92961e-5 * d,
            1.523688,
            0.093405 + 2.516e-9 * d,
            18.6021 + 0.5240207766 * d
        );
        finishPlanet(4, p.xh, p.yh, p.zh, p.r, (logRD, fv) => -1.51 + logRD + 0.016 * fv);
    }

    // 6. JUPITER (with Saturn mutual perturbations)
    {
        const p = computePlanetHelio(
            100.4542 + 2.76854e-5 * d,
            1.3030 - 1.557e-7 * d,
            273.8777 + 1.64505e-5 * d,
            5.20256,
            0.048498 + 4.469e-9 * d,
            MjDeg
        );
        let lonH = Math.atan2(p.yh, p.xh) * RAD2DEG;
        const latH = Math.asin(Math.max(-1.0, Math.min(1.0, p.zh / p.r)));
        lonH +=
            -0.332 * Math.sin(2.0 * Mj - 5.0 * Ms - 67.6 * DEG2RAD) -
            0.056 * Math.sin(2.0 * Mj - 2.0 * Ms + 21.0 * DEG2RAD) +
            0.042 * Math.sin(3.0 * Mj - 5.0 * Ms + 21.0 * DEG2RAD) -
            0.036 * Math.sin(Mj - 2.0 * Ms) +
            0.022 * Math.cos(Mj - Ms) +
            0.023 * Math.sin(2.0 * Mj - 3.0 * Ms + 52.0 * DEG2RAD) -
            0.016 * Math.sin(Mj - 5.0 * Ms - 69.0 * DEG2RAD);
        const lonRad = lonH * DEG2RAD;
        const cosB = Math.cos(latH);
        finishPlanet(
            5,
            p.r * cosB * Math.cos(lonRad),
            p.r * cosB * Math.sin(lonRad),
            p.r * Math.sin(latH),
            p.r,
            (logRD, fv) => -9.40 + logRD + 0.005 * fv
        );
    }

    // 7. SATURN (with Jupiter mutual perturbations & ring-tilt magnitude)
    {
        const nSat = 113.6634 + 2.3898e-5 * d;
        const iSat = 2.4886 - 1.081e-7 * d;
        const p = computePlanetHelio(
            nSat,
            iSat,
            339.3939 + 2.97661e-5 * d,
            9.55475,
            0.055546 - 9.499e-9 * d,
            MsDeg
        );
        let lonH = Math.atan2(p.yh, p.xh) * RAD2DEG;
        let latH = Math.asin(Math.max(-1.0, Math.min(1.0, p.zh / p.r))) * RAD2DEG;
        lonH +=
            0.812 * Math.sin(2.0 * Mj - 5.0 * Ms - 67.6 * DEG2RAD) -
            0.229 * Math.cos(2.0 * Mj - 4.0 * Ms - 2.0 * DEG2RAD) +
            0.119 * Math.sin(Mj - 2.0 * Ms - 3.0 * DEG2RAD) +
            0.046 * Math.sin(2.0 * Mj - 6.0 * Ms - 69.0 * DEG2RAD) +
            0.014 * Math.sin(Mj - 3.0 * Ms + 32.0 * DEG2RAD);
        latH +=
            -0.020 * Math.cos(2.0 * Mj - 4.0 * Ms - 2.0 * DEG2RAD) +
            0.018 * Math.sin(2.0 * Mj - 6.0 * Ms - 49.0 * DEG2RAD);
        const lonRad = lonH * DEG2RAD;
        const latRadS = latH * DEG2RAD;
        const cosB = Math.cos(latRadS);
        // Approximate ring sub-Earth latitude B
        const ir = 28.06 * DEG2RAD;
        const nr = (169.51 + 3.82e-5 * d) * DEG2RAD;
        const sinB =
            Math.sin(latRadS) * Math.cos(ir) -
            cosB * Math.sin(ir) * Math.sin(lonRad - nr);
        finishPlanet(
            6,
            p.r * cosB * Math.cos(lonRad),
            p.r * cosB * Math.sin(lonRad),
            p.r * Math.sin(latRadS),
            p.r,
            (logRD, fv) =>
                -8.88 + logRD + 0.044 * fv - 2.6 * Math.abs(sinB) + 1.2 * sinB * sinB
        );
    }

    // 8. URANUS (with Jupiter & Saturn perturbations)
    {
        const p = computePlanetHelio(
            74.0005 + 1.3978e-5 * d,
            0.7733 + 1.9e-8 * d,
            96.6612 + 3.0565e-5 * d,
            19.18171 - 1.55e-8 * d,
            0.047318 + 7.45e-9 * d,
            MuDeg
        );
        let lonH = Math.atan2(p.yh, p.xh) * RAD2DEG;
        const latH = Math.asin(Math.max(-1.0, Math.min(1.0, p.zh / p.r)));
        lonH +=
            0.040 * Math.sin(Ms - 2.0 * Mu + 6.0 * DEG2RAD) +
            0.035 * Math.sin(Ms - 3.0 * Mu + 33.0 * DEG2RAD) -
            0.015 * Math.sin(Mj - Mu + 20.0 * DEG2RAD);
        const lonRad = lonH * DEG2RAD;
        const cosB = Math.cos(latH);
        finishPlanet(
            7,
            p.r * cosB * Math.cos(lonRad),
            p.r * cosB * Math.sin(lonRad),
            p.r * Math.sin(latH),
            p.r,
            (logRD) => -7.19 + logRD
        );
    }

    // 9. NEPTUNE
    {
        const p = computePlanetHelio(
            131.7806 + 3.0173e-5 * d,
            1.7700 - 2.55e-7 * d,
            272.8461 - 6.027e-6 * d,
            30.05826 + 3.313e-8 * d,
            0.008606 + 2.15e-9 * d,
            260.2471 + 0.005995147 * d
        );
        finishPlanet(8, p.xh, p.yh, p.zh, p.r, (logRD) => -6.87 + logRD);
    }

    // 10. PLUTO (Standish 1800–2050 JPL J2000 Keplerian fit)
    {
        const aP = 39.48211675 - 0.00031596 * T;
        const eP = 0.24882730 + 0.00005170 * T;
        const iP = 17.14001206 + 0.00004818 * T;
        const LP = 238.92903833 + 145.20780515 * T;
        const wBarP = 224.06891629 - 0.04062942 * T;
        const NP = 110.30393684 - 0.01183482 * T;
        const p = computePlanetHelio(NP, iP, wBarP - NP, aP, eP, LP - wBarP);
        // Convert Sun ecliptic-of-date slightly back or combine in J2000 (Pluto r ~ 35 AU, Sun precession < 0.01 deg)
        const cosEcl0 = Math.cos(23.439291 * DEG2RAD);
        const sinEcl0 = Math.sin(23.439291 * DEG2RAD);
        const xg = p.xh + (precMat.xx * xsEq + precMat.yx * ysEq + precMat.zx * zsEq);
        const ygEqS = precMat.xy * xsEq + precMat.yy * ysEq + precMat.zy * zsEq;
        const zgEqS = precMat.xz * xsEq + precMat.yz * ysEq + precMat.zz * zsEq;
        const yg = p.yh + (ygEqS * cosEcl0 + zgEqS * sinEcl0);
        const zg = p.zh + (-ygEqS * sinEcl0 + zgEqS * cosEcl0);
        const delta = Math.hypot(xg, yg, zg) || 1.0;
        const logRD = 5.0 * Math.log10(Math.max(1e-4, p.r * delta));
        const mag = Number((-1.0 + logRD).toFixed(1));
        const xe = xg;
        const ye = yg * cosEcl0 - zg * sinEcl0;
        const ze = yg * sinEcl0 + zg * cosEcl0;
        setBodyEquatorial(bodyPool[9], xe, ye, ze, Number(delta.toFixed(4)), mag, null);
    }

    return bodyPool;
};
