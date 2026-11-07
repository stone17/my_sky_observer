/**
 * Utilities for parsing and evaluating local HH:MM observation time windows.
 */

export function formatIsoToHHMM(isoStr) {
    if (!isoStr) return '';
    const d = new Date(isoStr);
    if (isNaN(d.getTime())) return '';
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    return `${hh}:${mm}`;
}

export function parseHHMMToMinutes(timeStr) {
    if (!timeStr || typeof timeStr !== 'string') return null;
    const parts = timeStr.trim().split(':');
    if (parts.length < 2) return null;
    const hours = parseInt(parts[0], 10);
    const minutes = parseInt(parts[1], 10);
    if (isNaN(hours) || isNaN(minutes)) return null;
    if (hours === 24 && minutes === 0) return 0;
    if (hours < 0 || hours >= 24 || minutes < 0 || minutes >= 60) return null;
    return hours * 60 + minutes;
}

export function getAstroNightDefaults(nightTimes) {
    const startIso = nightTimes?.night?.[0];
    const endIso = nightTimes?.night?.[1];
    return {
        start: formatIsoToHHMM(startIso) || '18:00',
        end: formatIsoToHHMM(endIso) || '00:00'
    };
}

export function resolveEffectiveTimeWindow(clientSettings, nightTimes) {
    const active = Boolean(clientSettings?.filter_active_time_window);
    const defaults = getAstroNightDefaults(nightTimes);
    const rawStart = (clientSettings?.time_window_start || '').trim();
    const rawEnd = (clientSettings?.time_window_end || '').trim();
    const start = rawStart || defaults.start;
    const end = rawEnd || defaults.end;
    const startMin = parseHHMMToMinutes(start);
    const endMin = parseHHMMToMinutes(end);
    return {
        active,
        start,
        end,
        startMin,
        endMin
    };
}

export function isTimestampInTimeWindow(timeVal, startMin, endMin) {
    if (startMin === null || endMin === null) return true;
    const d = timeVal instanceof Date ? timeVal : new Date(timeVal);
    if (isNaN(d.getTime())) return true;
    const mins = d.getHours() * 60 + d.getMinutes();
    if (startMin <= endMin) {
        return mins >= startMin && mins <= endMin;
    }
    return mins >= startMin || mins <= endMin;
}
