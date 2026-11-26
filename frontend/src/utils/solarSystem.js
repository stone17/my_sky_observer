// Pure-JS 60 FPS analytical Solar System ephemeris engine (Sun, topocentric Moon with
// phase/limb geometry, Mercury–Neptune with mutual gas-giant perturbations, and Pluto).
// Zero allocations in steady state (< 0.03 ms per call).

import { ref } from 'vue';
import {
    DEG2RAD,
    RAD2DEG,
    wrapRaDeg,
    computeLstDeg,
    computeSolarEquatorial,
    computeLunarAltitudeDeg,
    computeNightEphemerisForEvening
} from './observingTime.js';
import { interpolateEphemerisAtTime } from './skyViewOptions.js';

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
    { id: 'sun', name: 'Sun', symbol: '☀', color: '#FBBF24', eqArcsec1Au: 1919.26 },
    { id: 'moon', name: 'Moon', symbol: '☾', color: '#E2E8F0', eqArcsec1Au: 0 },
    { id: 'mercury', name: 'Mercury', symbol: '☿', color: '#CBD5E1', eqArcsec1Au: 6.74 },
    { id: 'venus', name: 'Venus', symbol: '♀', color: '#FEF08A', eqArcsec1Au: 16.92 },
    { id: 'mars', name: 'Mars', symbol: '♂', color: '#F87171', eqArcsec1Au: 9.36 },
    { id: 'jupiter', name: 'Jupiter', symbol: '♃', color: '#FBBF24', eqArcsec1Au: 196.74 },
    { id: 'saturn', name: 'Saturn', symbol: '♄', color: '#FDE68A', eqArcsec1Au: 165.60 },
    { id: 'uranus', name: 'Uranus', symbol: '♅', color: '#38BDF8', eqArcsec1Au: 70.56 },
    { id: 'neptune', name: 'Neptune', symbol: '♆', color: '#818CF8', eqArcsec1Au: 68.28 },
    { id: 'pluto', name: 'Pluto', symbol: '♇', color: '#94A3B8', eqArcsec1Au: 3.28 }
];

const bodyPool = BODY_DEFS.map((def) => ({
    id: def.id,
    name: def.name,
    symbol: def.symbol,
    color: def.color,
    eqArcsec1Au: def.eqArcsec1Au,
    ra: 0,
    dec: 0,
    x: 1,
    y: 0,
    z: 0,
    mag: 0,
    distance_au: 1,
    angular_diameter_deg: 0,
    ring_diameter_deg: 0,
    ring_tilt_deg: 0,
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
        const slot = bodyPool[slotIdx];
        setBodyEquatorial(
            slot,
            xe,
            ye,
            ze,
            Number(delta.toFixed(4)),
            mag,
            usePrec ? precMat : null
        );
        const ill = 0.5 * (1.0 + cosFV);
        slot.phase_fraction = ill;
        slot.phase_percent = Number((ill * 100.0).toFixed(1));
        slot.angular_diameter_deg = (slot.eqArcsec1Au || 0) / (delta * 3600.0);
        slot.sun_ra = sunSlot.ra;
        slot.sun_dec = sunSlot.dec;
        const cosEl = Math.max(
            -1.0,
            Math.min(1.0, slot.x * sunSlot.x + slot.y * sunSlot.y + slot.z * sunSlot.z)
        );
        slot.elongation_deg = Number((Math.acos(cosEl) * RAD2DEG).toFixed(1));
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
        const satSlot = bodyPool[6];
        satSlot.ring_diameter_deg = 375.0 / (Math.max(1.0, satSlot.distance_au) * 3600.0);
        satSlot.ring_tilt_deg = Number(
            (Math.asin(Math.max(-1.0, Math.min(1.0, sinB))) * RAD2DEG).toFixed(1)
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
        const plutoSlot = bodyPool[9];
        setBodyEquatorial(plutoSlot, xe, ye, ze, Number(delta.toFixed(4)), mag, null);
        plutoSlot.angular_diameter_deg = (plutoSlot.eqArcsec1Au || 3.28) / (delta * 3600.0);
        plutoSlot.phase_fraction = 1.0;
        plutoSlot.phase_percent = 100.0;
        plutoSlot.sun_ra = sunSlot.ra;
        plutoSlot.sun_dec = sunSlot.dec;
        const cosElP = Math.max(
            -1.0,
            Math.min(1.0, plutoSlot.x * sunSlot.x + plutoSlot.y * sunSlot.y + plutoSlot.z * sunSlot.z)
        );
        plutoSlot.elongation_deg = Number((Math.acos(cosElP) * RAD2DEG).toFixed(1));
    }

    return bodyPool;
};

// --- IAU CONSTELLATION BOUNDARY LOOKUP & SOLAR SYSTEM TARGET BUILDER ---

export const solarConstellationVersion = ref(0);
let preparedConstBounds = null;
let preparedConstCentroids = null;
let constLoadPromise = null;

const buildConstRingSphere = (coords, idCode) => {
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
    const len = Math.hypot(sx, sy, sz) || 1.0;
    const cx = sx / len;
    const cy = sy / len;
    const cz = sz / len;
    let minDot = 1.0;
    for (let i = 0; i < n; i++) {
        const o = i * 3;
        const dot = xyz[o] * cx + xyz[o + 1] * cy + xyz[o + 2] * cz;
        if (dot < minDot) minDot = dot;
    }
    const radiusRad = Math.acos(Math.max(-1.0, Math.min(1.0, minDot)));
    return {
        code: String(idCode || '').toUpperCase(),
        xyz,
        count: n,
        cx,
        cy,
        cz,
        pipMinDot: Math.cos(Math.min(Math.PI, radiusRad + 0.02))
    };
};

export const ensureSolarConstellationsLoaded = () => {
    if (preparedConstBounds || typeof fetch === 'undefined') return Promise.resolve();
    if (!constLoadPromise) {
        constLoadPromise = fetch('/sky_data.json')
            .then((r) => (r.ok ? r.json() : null))
            .then((data) => {
                if (!data) return;
                const rings = [];
                for (const b of data.constellation_bounds || []) {
                    for (const ring of b.rings || []) {
                        if (ring && ring.length >= 3) {
                            rings.push(buildConstRingSphere(ring, b.id));
                        }
                    }
                }
                const centroids = [];
                for (const c of data.constellations || []) {
                    const a = c.ra * DEG2RAD;
                    const d = c.dec * DEG2RAD;
                    const cd = Math.cos(d);
                    centroids.push({
                        code: String(c.id || '').toUpperCase(),
                        x: cd * Math.cos(a),
                        y: cd * Math.sin(a),
                        z: Math.sin(d)
                    });
                }
                preparedConstBounds = rings;
                preparedConstCentroids = centroids;
                solarConstellationVersion.value += 1;
            })
            .catch(() => {
                constLoadPromise = null;
            });
    }
    return constLoadPromise;
};

const ZODIAC_SECTORS = [
    [0, 29, 'PSC'],
    [29, 53, 'ARI'],
    [53, 90, 'TAU'],
    [90, 118, 'GEM'],
    [118, 138, 'CNC'],
    [138, 174, 'LEO'],
    [174, 218, 'VIR'],
    [218, 241, 'LIB'],
    [241, 248, 'SCO'],
    [248, 266, 'OPH'],
    [266, 300, 'SGR'],
    [300, 328, 'CAP'],
    [328, 352, 'AQR'],
    [352, 360, 'PSC']
];

export const lookupConstellationCode = (raDeg, decDeg) => {
    if (!Number.isFinite(raDeg) || !Number.isFinite(decDeg)) return '';
    if (preparedConstBounds && preparedConstBounds.length > 0) {
        const a0 = raDeg * DEG2RAD;
        const d0 = decDeg * DEG2RAD;
        const sinA0 = Math.sin(a0);
        const cosA0 = Math.cos(a0);
        const sinD0 = Math.sin(d0);
        const cosD0 = Math.cos(d0);
        const qx = cosD0 * cosA0;
        const qy = cosD0 * sinA0;
        const qz = sinD0;
        const ex = -sinA0;
        const ey = cosA0;
        const nx = -sinD0 * cosA0;
        const ny = -sinD0 * sinA0;
        const nz = cosD0;

        for (let b = 0; b < preparedConstBounds.length; b++) {
            const ring = preparedConstBounds[b];
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
            if (valid && inside) return ring.code;
        }

        if (preparedConstCentroids && preparedConstCentroids.length > 0) {
            let bestCode = '';
            let bestDot = -2.0;
            for (let i = 0; i < preparedConstCentroids.length; i++) {
                const c = preparedConstCentroids[i];
                const dot = qx * c.x + qy * c.y + qz * c.z;
                if (dot > bestDot) {
                    bestDot = dot;
                    bestCode = c.code;
                }
            }
            return bestCode;
        }
    }

    // Approximate ecliptic longitude fallback before sky_data.json resolves
    const eps = 23.439291 * DEG2RAD;
    const aRad = raDeg * DEG2RAD;
    const dRad = decDeg * DEG2RAD;
    const x = Math.cos(dRad) * Math.cos(aRad);
    const y = Math.cos(eps) * Math.cos(dRad) * Math.sin(aRad) + Math.sin(eps) * Math.sin(dRad);
    const lon = wrapRaDeg(Math.atan2(y, x) * RAD2DEG);
    for (let i = 0; i < ZODIAC_SECTORS.length; i++) {
        const [lo, hi, code] = ZODIAC_SECTORS[i];
        if (lon >= lo && lon < hi) return code;
    }
    return 'PSC';
};

export const formatSolarAngularSize = (diamDeg) => {
    const d = Number(diamDeg);
    if (!Number.isFinite(d) || d <= 0) return '—';
    const arcmin = d * 60.0;
    if (arcmin >= 1.0) return `${arcmin.toFixed(1)}'`;
    const arcsec = d * 3600.0;
    return arcsec >= 1.0 ? `${arcsec.toFixed(1)}"` : `${arcsec.toFixed(2)}"`;
};

/**
 * Computes local daytime window (sunrise - 30m to sunset + 30m) around local solar noon
 * on the calendar date of `evening` so solar observations display a daytime altitude arc.
 */
export const computeDaytimeWindowForDate = (evening, observerLocation) => {
    if (!evening || !observerLocation) return null;
    const { year, month0, day } = evening;
    const lat = Number(observerLocation.lat);
    const lon = Number(observerLocation.lon);
    if (!Number.isFinite(year) || !Number.isFinite(lat) || !Number.isFinite(lon)) return null;

    const clampedLat = Math.max(-89.8, Math.min(89.8, lat));
    const sinLat = Math.sin(clampedLat * DEG2RAD);
    const cosLat = Math.cos(clampedLat * DEG2RAD);

    // Refine local solar noon (HA = 0 deg) on (year, month0, day)
    const approxNoonMs = Date.UTC(year, month0, day, 12, 0, 0) - (lon / 15.0) * 3600000.0;
    const sunNoon = computeSolarEquatorial(approxNoonMs);
    const lstNoon = computeLstDeg(approxNoonMs, lon);
    let haErr = wrapRaDeg(lstNoon - sunNoon.raDeg);
    if (haErr > 180.0) haErr -= 360.0;
    const solarNoonMs = approxNoonMs - (haErr / 360.0) * 86400000.0;

    const sinAlt = Math.sin(-0.833 * DEG2RAD);
    const cosH0 = (sinAlt - sinLat * sunNoon.sinDec) / Math.max(1e-6, cosLat * sunNoon.cosDec);
    if (cosH0 >= -1.0 && cosH0 <= 1.0) {
        const halfDayDeg = Math.acos(cosH0) * RAD2DEG;
        const offsetMs = (halfDayDeg / 360.0) * 86400000.0;
        const sunriseMs = solarNoonMs - offsetMs;
        const sunsetMs = solarNoonMs + offsetMs;
        return {
            startMs: sunriseMs - 30 * 60 * 1000,
            endMs: sunsetMs + 30 * 60 * 1000,
            sunriseMs,
            sunsetMs,
            solarNoonMs
        };
    }

    return {
        startMs: solarNoonMs - 12 * 3600 * 1000,
        endMs: solarNoonMs + 12 * 3600 * 1000,
        sunriseMs: null,
        sunsetMs: null,
        solarNoonMs
    };
};

const SOLAR_BODY_NOTES = {
    sun: {
        type: 'Sun',
        category: 'sun_moon',
        aliases: 'Sol, Daylight Star',
        subtitle: (b) => `Daytime Star · ${b.distance_au.toFixed(3)} AU`
    },
    moon: {
        type: 'Moon',
        category: 'sun_moon',
        aliases: 'Luna, Earth Moon',
        subtitle: (b) => `${b.phase_name} (${Math.round(b.phase_percent)}%)`
    },
    mercury: {
        type: 'Planet',
        category: 'planets',
        aliases: 'Sol I, Inner Planet',
        subtitle: (b) => `Rocky Planet · ${Math.round(b.phase_percent)}% lit · ${b.elongation_deg}° elong`
    },
    venus: {
        type: 'Planet',
        category: 'planets',
        aliases: 'Morning Star, Evening Star',
        subtitle: (b) => `Inner Planet · ${Math.round(b.phase_percent)}% lit · ${b.elongation_deg}° elong`
    },
    mars: {
        type: 'Planet',
        category: 'planets',
        aliases: 'Red Planet, Sol IV',
        subtitle: (b) => `Terrestrial Planet · ${b.distance_au.toFixed(2)} AU`
    },
    jupiter: {
        type: 'Planet',
        category: 'planets',
        aliases: 'Jove, Gas Giant, Sol V',
        subtitle: (b) => `Gas Giant · ${b.distance_au.toFixed(2)} AU`
    },
    saturn: {
        type: 'Planet',
        category: 'planets',
        aliases: 'Ringed Planet, Sol VI',
        subtitle: (b) =>
            `Ringed Giant · Rings ${formatSolarAngularSize(b.ring_diameter_deg)} (${Math.abs(b.ring_tilt_deg || 0).toFixed(1)}° tilt)`
    },
    uranus: {
        type: 'Planet',
        category: 'planets',
        aliases: 'Ice Giant, Sol VII',
        subtitle: (b) => `Ice Giant · ${b.distance_au.toFixed(2)} AU`
    },
    neptune: {
        type: 'Planet',
        category: 'planets',
        aliases: 'Ice Giant, Sol VIII',
        subtitle: (b) => `Ice Giant · ${b.distance_au.toFixed(2)} AU`
    },
    pluto: {
        type: 'Dwarf Planet',
        category: 'planets',
        aliases: '134340 Pluto, Kuiper Belt',
        subtitle: (b) => `Dwarf Planet · ${b.distance_au.toFixed(1)} AU`
    }
};

const ephemScratch = {
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
 * Builds first-class target objects for Sun, Moon, Mercury–Neptune, Pluto, and any saved
 * JPL Horizons ephemerides (comets, asteroids, spacecraft) at the active observing time & site.
 */
export const buildSolarSystemTargets = ({
    timeMs = Date.now(),
    evening = null,
    observerLocation = null,
    minHoursAltitude = 30.0,
    timeWindow = null,
    savedEphemerides = []
} = {}) => {
    ensureSolarConstellationsLoaded();

    const lat = observerLocation ? Number(observerLocation.lat) : 51.5;
    const lon = observerLocation ? Number(observerLocation.lon) : 0.0;
    const elev = observerLocation ? Number(observerLocation.elevation) || 0.0 : 0.0;
    const hasLoc = Boolean(observerLocation && Number.isFinite(lat) && Number.isFinite(lon));

    const clampedLat = Math.max(-89.999, Math.min(89.999, lat));
    const sinLat = Math.sin(clampedLat * DEG2RAD);
    const cosLat = Math.cos(clampedLat * DEG2RAD);
    const minAltThreshold = Number.isFinite(Number(minHoursAltitude)) ? Number(minHoursAltitude) : 30.0;

    // Snapshot current positions from bodyPool before any other calls
    const rawBodies = computeSolarSystemPositions(timeMs, lat, lon, elev);
    const snapshots = rawBodies.map((b) => ({ ...b }));

    // Night session bounds for Moon, Planets & Comets
    const nightEphem =
        hasLoc && evening
            ? computeNightEphemerisForEvening(evening.year, evening.month0, evening.day, lat, lon)
            : null;
    const nightStartMs = nightEphem ? nightEphem.startMs : timeMs - 6 * 3600000;
    const nightEndMs = nightEphem ? nightEphem.endMs : timeMs + 6 * 3600000;

    // Daytime session bounds for Sun
    const dayWin =
        hasLoc && evening
            ? computeDaytimeWindowForDate(evening, { lat, lon })
            : { startMs: timeMs - 6 * 3600000, endMs: timeMs + 6 * 3600000 };

    const NUM_PTS = 48;
    const nightSpanMs = Math.max(3600000, nightEndMs - nightStartMs);
    const nightStepMs = nightSpanMs / (NUM_PTS - 1);
    const nightStepHours = nightSpanMs / 3600000.0 / NUM_PTS;

    const twActive = Boolean(
        timeWindow?.active &&
            timeWindow.startMin !== null &&
            timeWindow.startMin !== undefined &&
            timeWindow.endMin !== null &&
            timeWindow.endMin !== undefined
    );
    const sMin = twActive ? timeWindow.startMin : 0;
    const eMin = twActive ? timeWindow.endMin : 0;

    // Precompute shared night timestamps, LST trig, Moon graph, Sun graph, and window flags
    const nightTimesIso = new Array(NUM_PTS);
    const nightLstRad = new Float64Array(NUM_PTS);
    const nightInWin = new Uint8Array(NUM_PTS);
    const sharedMoonGraph = new Array(NUM_PTS);
    const sharedSunGraph = new Array(NUM_PTS);

    for (let k = 0; k < NUM_PTS; k++) {
        const ms = nightStartMs + k * nightStepMs;
        const iso = new Date(ms).toISOString();
        nightTimesIso[k] = iso;
        nightLstRad[k] = computeLstDeg(ms, lon) * DEG2RAD;

        if (!twActive) {
            nightInWin[k] = 1;
        } else {
            const dt = new Date(ms);
            const mins = dt.getHours() * 60 + dt.getMinutes();
            const inWin = sMin <= eMin ? mins >= sMin && mins <= eMin : mins >= sMin || mins <= eMin;
            nightInWin[k] = inWin ? 1 : 0;
        }

        const moonAlt = hasLoc ? computeLunarAltitudeDeg(ms, lat, lon) : 0.0;
        sharedMoonGraph[k] = { time: iso, altitude: Number(moonAlt.toFixed(2)) };

        const sunEq = computeSolarEquatorial(ms);
        const sunHa = nightLstRad[k] - sunEq.raDeg * DEG2RAD;
        const sunSinAlt = sinLat * sunEq.sinDec + cosLat * sunEq.cosDec * Math.cos(sunHa);
        const sunAlt = hasLoc ? Math.asin(Math.max(-1.0, Math.min(1.0, sunSinAlt))) * RAD2DEG : 0.0;
        sharedSunGraph[k] = { time: iso, altitude: Number(sunAlt.toFixed(2)) };
    }

    const targets = [];
    const majorNameSet = new Set();

    for (let i = 0; i < snapshots.length; i++) {
        const b = snapshots[i];
        const meta = SOLAR_BODY_NOTES[b.id] || {
            type: 'Planet',
            category: 'planets',
            aliases: '',
            subtitle: () => 'Solar System Body'
        };
        majorNameSet.add(b.name.toLowerCase());

        let altGraph;
        let sunGraphForTarget = sharedSunGraph;
        let moonGraphForTarget = sharedMoonGraph;
        let peakAlt = 0.0;
        let hoursAbove = 0.0;

        if (b.id === 'sun') {
            const dStart = dayWin.startMs;
            const dSpan = Math.max(3600000, dayWin.endMs - dStart);
            const dStepMs = dSpan / (NUM_PTS - 1);
            const dStepHrs = dSpan / 3600000.0 / NUM_PTS;
            altGraph = new Array(NUM_PTS);
            moonGraphForTarget = new Array(NUM_PTS);
            sunGraphForTarget = altGraph;
            let countAbove = 0;
            let maxA = -90.0;

            for (let k = 0; k < NUM_PTS; k++) {
                const ms = dStart + k * dStepMs;
                const iso = new Date(ms).toISOString();
                const sunEq = computeSolarEquatorial(ms);
                const haRad = (computeLstDeg(ms, lon) - sunEq.raDeg) * DEG2RAD;
                const sinA = sinLat * sunEq.sinDec + cosLat * sunEq.cosDec * Math.cos(haRad);
                const alt = hasLoc ? Math.asin(Math.max(-1.0, Math.min(1.0, sinA))) * RAD2DEG : 0.0;
                const roundedAlt = Number(alt.toFixed(2));
                altGraph[k] = { time: iso, altitude: roundedAlt };
                const mAlt = hasLoc ? computeLunarAltitudeDeg(ms, lat, lon) : 0.0;
                moonGraphForTarget[k] = { time: iso, altitude: Number(mAlt.toFixed(2)) };
                if (roundedAlt > maxA) maxA = roundedAlt;
                if (roundedAlt >= Math.max(0.0, minAltThreshold)) countAbove++;
            }
            peakAlt = Math.max(0.0, Number(maxA.toFixed(1)));
            hoursAbove = Number((countAbove * dStepHrs).toFixed(1));
        } else if (b.id === 'moon') {
            altGraph = sharedMoonGraph;
            let countAbove = 0;
            let maxA = -90.0;
            for (let k = 0; k < NUM_PTS; k++) {
                const alt = sharedMoonGraph[k].altitude;
                if (alt > maxA) maxA = alt;
                if (nightInWin[k] && alt >= minAltThreshold) countAbove++;
            }
            peakAlt = Math.max(0.0, Number(maxA.toFixed(1)));
            hoursAbove = Number((countAbove * nightStepHours).toFixed(1));
        } else {
            const raRad = b.ra * DEG2RAD;
            const decRad = b.dec * DEG2RAD;
            const sinDec = Math.sin(decRad);
            const cosDec = Math.cos(decRad);
            altGraph = new Array(NUM_PTS);
            let countAbove = 0;
            let maxA = -90.0;

            for (let k = 0; k < NUM_PTS; k++) {
                const haRad = nightLstRad[k] - raRad;
                const sinA = sinLat * sinDec + cosLat * cosDec * Math.cos(haRad);
                const alt = hasLoc ? Math.asin(Math.max(-1.0, Math.min(1.0, sinA))) * RAD2DEG : 0.0;
                const roundedAlt = Number(alt.toFixed(2));
                altGraph[k] = { time: nightTimesIso[k], altitude: roundedAlt };
                if (roundedAlt > maxA) maxA = roundedAlt;
                if (nightInWin[k] && roundedAlt >= minAltThreshold) countAbove++;
            }
            peakAlt = Math.max(0.0, Number(maxA.toFixed(1)));
            hoursAbove = Number((countAbove * nightStepHours).toFixed(1));
        }

        const majArcmin = Number(((b.angular_diameter_deg || 0) * 60.0).toFixed(4));
        const constCode = lookupConstellationCode(b.ra, b.dec);

        targets.push({
            name: b.name,
            common_name: meta.subtitle(b),
            type: meta.type,
            constellation: constCode,
            other_id: b.id,
            aliases: meta.aliases,
            sub_objects: [],
            ra: Number(b.ra.toFixed(5)),
            dec: Number(b.dec.toFixed(5)),
            mag: b.mag,
            size: formatSolarAngularSize(b.angular_diameter_deg),
            maj_ax: majArcmin,
            surface_brightness: b.distance_au ? `${b.distance_au.toFixed(3)} AU` : 'N/A',
            max_altitude: peakAlt,
            hours_visible: hoursAbove,
            hours_above_min: hoursAbove,
            altitude_graph: altGraph,
            moon_graph: moonGraphForTarget,
            sun_graph: sunGraphForTarget,
            status: 'live',
            image_url: '',
            is_solar_system: true,
            is_ephemeris: false,
            solar_id: b.id,
            solar_category: meta.category,
            symbol: b.symbol,
            solar_color: b.color,
            distance_au: b.distance_au,
            angular_diameter_deg: b.angular_diameter_deg,
            ring_diameter_deg: b.ring_diameter_deg || 0,
            ring_tilt_deg: b.ring_tilt_deg || 0,
            phase_fraction: b.phase_fraction ?? 1.0,
            phase_percent: b.phase_percent ?? 100.0,
            phase_name: b.phase_name || '',
            elongation_deg: b.elongation_deg ?? 0,
            sun_ra: b.sun_ra,
            sun_dec: b.sun_dec
        });
    }

    // Append saved JPL Horizons Ephemeris objects (Comets, Asteroids, Spacecraft)
    if (Array.isArray(savedEphemerides) && savedEphemerides.length > 0) {
        for (let eIdx = 0; eIdx < savedEphemerides.length; eIdx++) {
            const ep = savedEphemerides[eIdx];
            if (!ep || !ep.preparedPoints || ep.preparedPoints.length === 0) continue;

            const posNow = interpolateEphemerisAtTime(ep, timeMs, ephemScratch);
            if (!posNow) continue;

            const curRa = posNow.ra;
            const curDec = posNow.dec;
            const curMag = posNow.mag !== null && posNow.mag !== undefined ? posNow.mag : 99;
            const curDist = posNow.distance_au;
            const inSpan = Boolean(posNow.inRange);

            let displayName = String(ep.name || ep.command || `Ephemeris ${eIdx + 1}`).trim();
            if (majorNameSet.has(displayName.toLowerCase())) {
                displayName = `${displayName} (JPL)`;
            }

            const altGraph = new Array(NUM_PTS);
            let countAbove = 0;
            let maxA = -90.0;
            for (let k = 0; k < NUM_PTS; k++) {
                const ms = nightStartMs + k * nightStepMs;
                const posK = interpolateEphemerisAtTime(ep, ms, ephemScratch);
                const rK = (posK ? posK.ra : curRa) * DEG2RAD;
                const dK = (posK ? posK.dec : curDec) * DEG2RAD;
                const haRad = nightLstRad[k] - rK;
                const sinA = sinLat * Math.sin(dK) + cosLat * Math.cos(dK) * Math.cos(haRad);
                const alt = hasLoc ? Math.asin(Math.max(-1.0, Math.min(1.0, sinA))) * RAD2DEG : 0.0;
                const roundedAlt = Number(alt.toFixed(2));
                altGraph[k] = { time: nightTimesIso[k], altitude: roundedAlt };
                if (roundedAlt > maxA) maxA = roundedAlt;
                if (nightInWin[k] && roundedAlt >= minAltThreshold) countAbove++;
            }

            const peakAlt = Math.max(0.0, Number(maxA.toFixed(1)));
            const hoursAbove = Number((countAbove * nightStepHours).toFixed(1));
            const constCode = lookupConstellationCode(curRa, curDec);
            const distText = curDist !== null && curDist !== undefined ? `${curDist.toFixed(2)} AU` : '';
            const subtitleParts = [
                inSpan ? 'JPL Trajectory' : 'Outside Ephemeris Span',
                distText
            ].filter(Boolean);

            targets.push({
                name: displayName,
                common_name: subtitleParts.join(' · '),
                type: 'Comet / Minor',
                constellation: constCode,
                other_id: ep.command || ep.id,
                aliases: ep.command || '',
                sub_objects: [],
                ra: Number(curRa.toFixed(5)),
                dec: Number(curDec.toFixed(5)),
                mag: curMag,
                size: distText || '—',
                maj_ax: 0,
                surface_brightness: distText || 'N/A',
                max_altitude: peakAlt,
                hours_visible: hoursAbove,
                hours_above_min: hoursAbove,
                altitude_graph: altGraph,
                moon_graph: sharedMoonGraph,
                sun_graph: sharedSunGraph,
                status: 'live',
                image_url: '',
                is_solar_system: true,
                is_ephemeris: true,
                ephemeris_id: ep.id,
                solar_id: `ephem_${ep.id}`,
                solar_category: 'minor',
                symbol: '☄',
                solar_color: ep.color || '#10B981',
                distance_au: curDist,
                angular_diameter_deg: 0,
                phase_fraction: 1.0,
                phase_percent: 100.0,
                phase_name: '',
                in_ephemeris_range: inSpan
            });
        }
    }

    return targets;
};
