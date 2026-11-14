"""
Build compact offline celestial dataset (stars, star names, constellations, boundaries, Milky Way)
for the My Sky Observer interactive Sky Context Map.

Output: frontend/public/sky_data.json
All celestial coordinates are normalized to J2000 Right Ascension [0, 360) deg and Declination [-90, +90] deg.
"""

import json
import os
import urllib.request

BASE_URL = "https://raw.githubusercontent.com/ofrohn/d3-celestial/master/data/"
OUTPUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "frontend",
    "public",
    "sky_data.json",
)


def fetch_json(filename: str):
    url = BASE_URL + filename
    print(f"Fetching {url} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "MySkyObserver/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def lon_to_ra_deg(lon: float) -> float:
    """Convert d3-celestial GeoJSON longitude [-180, 180] to J2000 RA [0, 360) deg."""
    ra = float(lon) % 360.0
    if ra < 0:
        ra += 360.0
    return round(ra, 4)


def round_dec(lat: float) -> float:
    return round(float(lat), 4)


def simplify_ring(coords, step: int = 2):
    if len(coords) <= 8:
        return [[lon_to_ra_deg(p[0]), round_dec(p[1])] for p in coords]
    pts = coords[:-1:step]
    if len(pts) < 4:
        pts = coords[:-1]
    out = [[lon_to_ra_deg(p[0]), round_dec(p[1])] for p in pts]
    if out and out[0] != out[-1]:
        out.append(list(out[0]))
    return out


def build_sky_data():
    stars_geo = fetch_json("stars.6.json")
    star_names = fetch_json("starnames.json")
    constellations_geo = fetch_json("constellations.json")
    const_lines_geo = fetch_json("constellations.lines.json")
    const_bounds_geo = fetch_json("constellations.bounds.json")
    mw_geo = fetch_json("mw.json")

    # 1. Process Stars
    # Compact array format per star: [ra, dec, mag, bv, name, desig]
    stars_out = []
    betelgeuse_check = None
    vega_check = None

    for feat in stars_geo.get("features", []):
        props = feat.get("properties", {})
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates", [])
        if len(coords) < 2:
            continue

        hip_id = str(feat.get("id", ""))
        ra = lon_to_ra_deg(coords[0])
        dec = round_dec(coords[1])
        mag = round(float(props.get("mag", 6.0)), 2)
        raw_bv = props.get("bv", 0.5)
        try:
            bv = round(float(raw_bv), 2) if raw_bv != "" and raw_bv is not None else 0.5
        except (TypeError, ValueError):
            bv = 0.5

        meta = star_names.get(hip_id, {})
        proper_name = str(meta.get("name", "") or "").strip()
        bayer = str(meta.get("bayer", "") or "").strip()
        const_abbr = str(meta.get("c", "") or "").strip()
        desig = f"{bayer} {const_abbr}".strip() if bayer and const_abbr else bayer

        if hip_id == "27989":
            betelgeuse_check = (ra, dec, mag, bv, proper_name)
        elif hip_id == "91262":
            vega_check = (ra, dec, mag, bv, proper_name)

        entry = [ra, dec, mag, bv]
        if proper_name or desig:
            entry.append(proper_name)
            if desig:
                entry.append(desig)
        stars_out.append(entry)

    # Sort brightest stars first so rendering & label collision prioritizes bright stars
    stars_out.sort(key=lambda s: s[2])

    print(f"Processed {len(stars_out)} stars.")
    print(f"  Check Betelgeuse (expect ~88.79, +7.41): {betelgeuse_check}")
    print(f"  Check Vega       (expect ~279.23, +38.78): {vega_check}")

    # 2. Process Constellations (metadata + centroids)
    constellations_out = []
    for feat in constellations_geo.get("features", []):
        cid = str(feat.get("id", ""))
        props = feat.get("properties", {})
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates", [])
        # display property sometimes holds [ra_lon, dec_lat, scale]
        disp = props.get("display")
        if isinstance(disp, list) and len(disp) >= 2:
            c_ra = lon_to_ra_deg(disp[0])
            c_dec = round_dec(disp[1])
        elif len(coords) >= 2:
            c_ra = lon_to_ra_deg(coords[0])
            c_dec = round_dec(coords[1])
        else:
            continue

        name = str(props.get("name") or props.get("en") or cid).strip()
        rank = int(props.get("rank", 2) or 2)
        constellations_out.append(
            {
                "id": cid,
                "name": name,
                "ra": c_ra,
                "dec": c_dec,
                "rank": rank,
            }
        )

    # 3. Process Constellation Stick Lines
    const_lines_out = []
    for feat in const_lines_geo.get("features", []):
        cid = str(feat.get("id", ""))
        geom = feat.get("geometry", {})
        gtype = geom.get("type")
        coords = geom.get("coordinates", [])
        segments = []
        if gtype == "MultiLineString":
            for seg in coords:
                segments.append([[lon_to_ra_deg(p[0]), round_dec(p[1])] for p in seg])
        elif gtype == "LineString":
            segments.append([[lon_to_ra_deg(p[0]), round_dec(p[1])] for p in coords])
        if segments:
            const_lines_out.append({"id": cid, "lines": segments})

    # 4. Process Constellation Boundaries
    const_bounds_out = []
    for feat in const_bounds_geo.get("features", []):
        cid = str(feat.get("id", ""))
        geom = feat.get("geometry", {})
        coords = geom.get("coordinates", [])
        gtype = geom.get("type")
        rings = []
        if gtype == "Polygon":
            for ring in coords:
                rings.append([[lon_to_ra_deg(p[0]), round_dec(p[1])] for p in ring])
        elif gtype == "MultiPolygon":
            for poly in coords:
                for ring in poly:
                    rings.append([[lon_to_ra_deg(p[0]), round_dec(p[1])] for p in ring])
        elif gtype == "LineString":
            rings.append([[lon_to_ra_deg(p[0]), round_dec(p[1])] for p in coords])
        elif gtype == "MultiLineString":
            for seg in coords:
                rings.append([[lon_to_ra_deg(p[0]), round_dec(p[1])] for p in seg])
        if rings:
            const_bounds_out.append({"id": cid, "rings": rings})

    # 5. Process Milky Way Polygons (keep subtle contours, lightly simplified for size)
    mw_out = []
    for idx, feat in enumerate(mw_geo.get("features", [])):
        if idx >= 3:
            # First 3 contours capture the main diffuse Galactic band & brighter core clouds cleanly
            break
        geom = feat.get("geometry", {})
        gtype = geom.get("type")
        coords = geom.get("coordinates", [])
        polys = []
        step = 3 if idx == 0 else 2
        if gtype == "MultiPolygon":
            for poly in coords:
                rings = [simplify_ring(ring, step=step) for ring in poly if len(ring) >= 4]
                if rings:
                    polys.append(rings)
        elif gtype == "Polygon":
            rings = [simplify_ring(ring, step=step) for ring in coords if len(ring) >= 4]
            if rings:
                polys.append(rings)
        if polys:
            mw_out.append({"level": idx, "polygons": polys})

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    payload = {
        "stars": stars_out,
        "constellations": constellations_out,
        "constellation_lines": const_lines_out,
        "constellation_bounds": const_bounds_out,
        "milky_way": mw_out,
    }
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)

    size_kb = os.path.getsize(OUTPUT_PATH) / 1024.0
    print(f"Wrote {OUTPUT_PATH} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    build_sky_data()
