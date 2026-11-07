"""
Build & enrich astronomical catalog CSVs for My Sky Observer.

Sources merged:
1. N.I.N.A. (Nighttime Imaging 'N' Astronomy) DSO SQLite database
   - initial_schema.sql, initial_data.sql, and Migration/1.sql .. 16.sql
2. Stellarium DSO common names (nebulae/default/names.dat)
3. OpenNGC (NGC.csv + addendum.csv) common names and cross-identifications
4. Existing local catalogs/*.csv (preserves any custom or Chandra SNR entries)
"""

import glob
import io
import math
import os
import re
import sqlite3
import urllib.request
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

NINA_BASE_URL = "https://raw.githubusercontent.com/isbeorn/nina/develop/NINA/Database/"
NINA_MIGRATIONS = [1, 2, 3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
STELLARIUM_NAMES_URL = "https://raw.githubusercontent.com/Stellarium/stellarium/master/nebulae/default/names.dat"
OPENNGC_MAIN_URL = "https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/NGC.csv"
OPENNGC_ADDENDUM_URL = "https://raw.githubusercontent.com/mattiaverga/OpenNGC/master/database_files/addendum.csv"

CATALOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "catalogs")

# Map NINA dsotype codes to My Sky Observer type codes
NINA_TYPE_MAP = {
    "BRTNB": "Brt Neb",
    "DRKNB": "Drk Neb",
    "LMCDN": "Drk Neb",
    "SMCDN": "Drk Neb",
    "PLNNB": "Pln Neb",
    "SNREM": "SN Rem",
    "CL+NB": "CL+Neb",
    "LMCCN": "CL+Neb",
    "SMCCN": "CL+Neb",
    "GALXY": "Galaxy",
    "GALCL": "Gal CL",
    "GLOCL": "Glob CL",
    "LMCGC": "Glob CL",
    "SMCGC": "Glob CL",
    "OPNCL": "Opn CL",
    "LMCOC": "Opn CL",
    "SMCOC": "Opn CL",
    "ASTER": "Aster",
    "3STAR": "Aster",
    "4STAR": "Aster",
    "1STAR": "Star",
    "2STAR": "2 Stars",
    "QUASR": "Quasar",
    "GX+DN": "Neb+Gx",
    "GX+GC": "Glo+Gx",
    "C+N+G": "C+N+G",
}

# Curated high-priority astrophotography common names to guarantee iconic targets have their best-known name
CURATED_COMMON_NAMES = {
    "M 1": "Crab Nebula",
    "M 6": "Butterfly Cluster",
    "M 7": "Ptolemy's Cluster",
    "M 8": "Lagoon Nebula",
    "M 11": "Wild Duck Cluster",
    "M 13": "Hercules Globular Cluster",
    "M 16": "Eagle Nebula",
    "M 17": "Omega Nebula",
    "M 20": "Trifid Nebula",
    "M 24": "Small Sagittarius Star Cloud",
    "M 27": "Dumbbell Nebula",
    "M 31": "Andromeda Galaxy",
    "M 33": "Triangulum Galaxy",
    "M 42": "Orion Nebula",
    "M 43": "De Mairan's Nebula",
    "M 44": "Beehive Cluster",
    "M 45": "Pleiades",
    "M 51": "Whirlpool Galaxy",
    "M 57": "Ring Nebula",
    "M 63": "Sunflower Galaxy",
    "M 64": "Black Eye Galaxy",
    "M 74": "Phantom Galaxy",
    "M 76": "Little Dumbbell Nebula",
    "M 78": "Casper the Friendly Ghost Nebula",
    "M 81": "Bode's Galaxy",
    "M 82": "Cigar Galaxy",
    "M 83": "Southern Pinwheel Galaxy",
    "M 97": "Owl Nebula",
    "M 101": "Pinwheel Galaxy",
    "M 104": "Sombrero Galaxy",
    "NGC 253": "Sculptor Galaxy",
    "NGC 281": "Pacman Nebula",
    "NGC 869": "Double Cluster (h Persei)",
    "NGC 884": "Double Cluster (chi Persei)",
    "NGC 891": "Silver Sliver Galaxy",
    "NGC 1333": "Embryo Nebula",
    "NGC 1499": "California Nebula",
    "NGC 1977": "Running Man Nebula",
    "NGC 2024": "Flame Nebula",
    "NGC 2070": "Tarantula Nebula",
    "NGC 2174": "Monkey Head Nebula",
    "NGC 2237": "Rosette Nebula",
    "NGC 2261": "Hubble's Variable Nebula",
    "NGC 2264": "Cone Nebula & Christmas Tree Cluster",
    "NGC 2359": "Thor's Helmet",
    "NGC 2392": "Eskimo Nebula",
    "NGC 2403": "Caldwell 7 Galaxy",
    "NGC 3242": "Ghost of Jupiter",
    "NGC 3372": "Carina Nebula",
    "NGC 3576": "Statue of Liberty Nebula",
    "NGC 4038": "Antennae Galaxies",
    "NGC 4565": "Needle Galaxy",
    "NGC 4631": "Whale Galaxy",
    "NGC 4656": "Hockey Stick Galaxy",
    "NGC 5128": "Centaurus A",
    "NGC 5139": "Omega Centauri",
    "NGC 6188": "Fighting Dragons of Ara",
    "NGC 6302": "Bug Nebula",
    "NGC 6334": "Cat's Paw Nebula",
    "NGC 6357": "Lobster Nebula",
    "NGC 6543": "Cat's Eye Nebula",
    "NGC 6820": " Vulpecula Pillar Nebula",
    "NGC 6822": "Barnard's Galaxy",
    "NGC 6888": "Crescent Nebula",
    "NGC 6946": "Fireworks Galaxy",
    "NGC 6960": "Western Veil Nebula (Witch's Broom)",
    "NGC 6979": "Pickering's Triangle",
    "NGC 6992": "Eastern Veil Nebula",
    "NGC 7000": "North America Nebula",
    "NGC 7009": "Saturn Nebula",
    "NGC 7023": "Iris Nebula",
    "NGC 7293": "Helix Nebula",
    "NGC 7331": "Deer Lick Group Galaxy",
    "NGC 7380": "Wizard Nebula",
    "NGC 7635": "Bubble Nebula",
    "NGC 7822": "Question Mark Nebula",
    "IC 342": "Hidden Galaxy",
    "IC 405": "Flaming Star Nebula",
    "IC 410": "Tadpoles Nebula",
    "IC 417": "Spider Nebula",
    "IC 434": "Horsehead & Flame Region",
    "IC 443": "Jellyfish Nebula",
    "IC 1318": "Sadr Region ( Butterfly Nebula )",
    "IC 1396": "Elephant's Trunk Nebula",
    "IC 1795": "Fishhead Nebula",
    "IC 1805": "Heart Nebula",
    "IC 1848": "Soul Nebula",
    "IC 2118": "Witch Head Nebula",
    "IC 2169": "Dreyer's Nebula",
    "IC 2177": "Seagull Nebula",
    "IC 2944": "Running Chicken Nebula",
    "IC 4604": "Rho Ophiuchi Cloud Complex",
    "IC 5067": "Pelican Nebula",
    "IC 5070": "Pelican Nebula",
    "IC 5146": "Cocoon Nebula",
    "SH2-101": "Tulip Nebula",
    "SH2-106": "Celestial Snow Angel Nebula",
    "SH2-114": "Flying Dragon Nebula",
    "SH2-129": "Flying Bat Nebula",
    "SH2-132": "Lion Nebula",
    "SH2-140": "Cepheus Bow Shock Nebula",
    "SH2-155": "Cave Nebula",
    "SH2-157": "Lobster Claw Nebula",
    "SH2-170": "Little Rosette Nebula",
    "SH2-171": "Teddy Bear Nebula",
    "SH2-188": "Dolphin-Shaped Planetary Nebula",
    "SH2-224": "Rice Hat Supernova Remnant",
    "SH2-240": "Spaghetti Nebula (Simeis 147)",
    "SH2-261": "Lower's Nebula",
    "SH2-274": "Medusa Nebula",
    "SH2-308": "Dolphin Head Nebula",
    "B 33": "Horsehead Nebula",
    "B 59": "Pipe Nebula (Stem)",
    "B 72": "Snake Nebula",
    "B 142": "Barnard's E Nebula",
    "B 143": "Barnard's E Nebula",
    "B 150": "Seahorse Nebula",
    "B 169": "bd+56 2617 Ring Dark Nebula",
    "B 174": "Cepheus Dark Cloud",
    "LDN 673": "Aquila Dark Rift",
    "LDN 1235": "Dark Shark Nebula",
    "LDN 1251": "Rotten Fish Nebula",
    "LDN 1355": "Helping Hand Nebula",
    "LDN 1622": "Boogeyman Nebula",
    "LBN 437": "Gecko Nebula",
    "LBN 552": "Cepheus Molecular Cloud",
    "LBN 777": "Baby Eagle Nebula (Vulture Head)",
    "VDB 141": "Ghost Nebula",
    "VDB 152": "Wolf's Cave Nebula",
    "CR 399": "Coathanger Cluster (Brocchi's Cluster)",
    "OU 4": "Giant Squid Nebula",
    "ABELL 21": "Medusa Nebula",
    "ABELL 31": "Ancient Planetary Nebula Abell 31",
    "ABELL 39": "Abell 39 Spherical Planetary Nebula",
    "ABELL 85": "CTB 1 (Garlic Nebula)",
}


def normalize_token(val: str) -> str:
    """Canonical token normalization matching CatalogManager.normalize_designation."""
    if not val or pd.isna(val):
        return ""
    s = str(val).strip()
    if not s or s.lower() in ("n/a", "nan", "-"):
        return ""
    s = re.sub(r"\s+", " ", s).upper()
    s = re.sub(r"^COLLINDER\s+", "CR ", s)
    s = re.sub(r"^BERKELEY\s+", "BERK ", s)
    s = re.sub(r"^RUPRECHT\s+", "RU ", s)
    s = re.sub(r"^SH\s*2[\-\s]+", "SH2-", s)
    s = re.sub(r"^BARNARD\s+", "B ", s)
    s = re.sub(r"^CALDWELL\s+", "C ", s)
    s = re.sub(r"^MELOTTE\s+", "MEL ", s)
    s = re.sub(r"^TRUMPLER\s+", "TR ", s)
    s = re.sub(r"^HICKSON\s+", "HCG ", s)
    s = re.sub(r"^ACO\s+", "ABELL ", s)
    # Strip leading zeros after prefix (e.g., NGC 0224 -> NGC 224, B033 -> B 33)
    m = re.match(r"^(M|NGC|IC|C|B|CR|BERK|RU|ABELL|UGC|LBN|LDN|VDB|ARP|HCG|MEL|TR|STOCK|GUM|RCW|OU|CED|DWB)\s*0*(\d+[A-Z]*)$", s)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    m_sh = re.match(r"^SH2-\s*0*(\d+[A-Z]*)$", s)
    if m_sh:
        return f"SH2-{m_sh.group(1)}"
    return s


def format_ra_string(ra_deg: float) -> str:
    """Format RA degrees to 'HH MM.M'."""
    if pd.isna(ra_deg):
        return "00 00.0"
    ra_h = (float(ra_deg) / 15.0) % 24.0
    hours = int(ra_h)
    minutes = (ra_h - hours) * 60.0
    if round(minutes, 1) >= 60.0:
        hours = (hours + 1) % 24
        minutes = 0.0
    return f"{hours:02d} {minutes:04.1f}"


def format_dec_string(dec_deg: float) -> str:
    """Format Dec degrees to '+DD MM'."""
    if pd.isna(dec_deg):
        return "+00 00"
    d = float(dec_deg)
    sign = "+" if d >= 0 else "-"
    d_abs = abs(d)
    deg = int(d_abs)
    arcmin = int(round((d_abs - deg) * 60.0))
    if arcmin >= 60:
        deg += 1
        arcmin = 0
    return f"{sign}{deg:02d} {arcmin:02d}"


def format_size_arcmin(sizemax_arcsec) -> str:
    """Convert NINA sizemax (arcseconds) to arcminutes string (e.g. \"15'\" or \"1.7'\")."""
    if sizemax_arcsec is None or pd.isna(sizemax_arcsec):
        return "N/A"
    try:
        val = float(sizemax_arcsec)
    except (ValueError, TypeError):
        return "N/A"
    if val <= 0:
        return "N/A"
    arcmin = val / 60.0
    if arcmin >= 10.0 and abs(arcmin - round(arcmin)) < 0.05:
        return f"{int(round(arcmin))}'"
    elif abs(arcmin - round(arcmin, 1)) < 0.01:
        s = f"{arcmin:.1f}".rstrip("0").rstrip(".")
        return f"{s}'"
    else:
        s = f"{arcmin:.2f}".rstrip("0").rstrip(".")
        return f"{s}'"


def format_mag(val) -> str:
    """Format magnitude or surface brightness from NINA DB."""
    if val is None or pd.isna(val):
        return "-"
    try:
        f = float(val)
    except (ValueError, TypeError):
        return "-"
    if f >= 70.0 or f <= -30.0:
        return "-"
    s = f"{f:.1f}".rstrip("0").rstrip(".")
    return s


def is_good_common_name(name: str) -> bool:
    """Filter out technical survey codes disguised as NAME in NINA DB (e.g. 'FIRSSE 213', 'SMAC ACO 569S')."""
    if not name or pd.isna(name):
        return False
    s = str(name).strip()
    if len(s) < 3 or s.lower() in ("n/a", "nan", "-"):
        return False
    # Exclude pure catalog numbers or obscure radio/IR source codes
    bad_prefixes = (
        "FIRSSE ", "SMAC ", "MWP1", "LDN ", "LBN ", "SH2-", "NGC ", "IC ",
        "PGC ", "UGC ", "MCG ", "ESO ", "2MASX ", "IRAS ", "PK ", "ACO ",
    )
    su = s.upper()
    if any(su.startswith(p) for p in bad_prefixes):
        return False
    if re.match(r"^[A-Z0-9\-\+]+$", s) and not any(w in su for w in ("NEB", "GAL", "CLUST", "CLOUD", "RING", "STAR")):
        return False
    return True


def score_common_name(name: str) -> Tuple[int, int, str]:
    """Score common names so the most descriptive, recognizable title-case name wins primary."""
    s = name.strip()
    is_all_caps = 1 if s.isupper() else 0
    has_descriptive_word = 0 if any(
        w in s.lower() for w in ("nebula", "galaxy", "cluster", "cloud", "remnant", "triplet", "quintet", "quartet", "sextet", "group", "pleiades", "hyades", "coalsack", "tarantula", "omega", "centauri", "helmet", "hand")
    ) else 1
    # Prefer standard length names over ultra-short abbreviations like "Crab" or "Tau A"
    is_short_radio = 1 if re.match(r"^[A-Za-z]{3}\s+[A-Z]$", s) else 0
    return (is_all_caps, is_short_radio, has_descriptive_word, len(s), s)


def fetch_stellarium_names() -> Dict[str, List[str]]:
    """Fetch Stellarium's nebulae/default/names.dat and map normalized designation -> list of common names."""
    print("Fetching Stellarium common names (names.dat)...")
    req = urllib.request.urlopen(STELLARIUM_NAMES_URL, timeout=60)
    raw = req.read().decode("utf-8", errors="replace")
    mapping: Dict[str, List[str]] = defaultdict(list)

    for line in raw.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        desig_part = line[:20].strip()
        m = re.search(r'_\("([^"]+)"\)', line)
        if not desig_part or not m:
            continue
        cname = m.group(1).strip()
        # Stellarium uses bare numbers for Caldwell or special catalogs? Wait: lines like "47  _("...")" are Ced or similar, skip bare numbers
        if re.match(r"^\d+$", desig_part):
            continue
        norm_d = normalize_token(desig_part)
        if norm_d and cname not in mapping[norm_d]:
            mapping[norm_d].append(cname)

    print(f"  -> Loaded common names for {len(mapping)} designations from Stellarium.")
    return mapping


def fetch_openngc_names() -> Dict[str, List[str]]:
    """Fetch OpenNGC NGC.csv and addendum.csv for common names."""
    print("Fetching OpenNGC common names...")
    mapping: Dict[str, List[str]] = defaultdict(list)

    for url in (OPENNGC_MAIN_URL, OPENNGC_ADDENDUM_URL):
        req = urllib.request.urlopen(url, timeout=60)
        raw = req.read().decode("utf-8", errors="replace")
        df = pd.read_csv(io.StringIO(raw), sep=";")
        for _, row in df.iterrows():
            cnames_raw = row.get("Common names")
            if pd.isna(cnames_raw) or not str(cnames_raw).strip():
                continue
            cnames = [c.strip() for c in str(cnames_raw).split(",") if c.strip()]
            if not cnames:
                continue

            # Collect all designations for this OpenNGC row
            tokens = [normalize_token(row.get("Name", ""))]
            m_val = row.get("M")
            if pd.notna(m_val) and str(m_val).strip():
                try:
                    tokens.append(f"M {int(float(m_val))}")
                except ValueError:
                    pass
            idents = row.get("Identifiers")
            if pd.notna(idents) and str(idents).strip():
                for part in str(idents).split(","):
                    tokens.append(normalize_token(part))

            for t in tokens:
                if not t:
                    continue
                for cn in cnames:
                    if is_good_common_name(cn) and cn not in mapping[t]:
                        mapping[t].append(cn)

    print(f"  -> Loaded common names for {len(mapping)} designations from OpenNGC.")
    return mapping


def build_nina_sqlite() -> sqlite3.Connection:
    """Download NINA SQL schema, initial_data, and migrations 1..16 into an in-memory SQLite DB."""
    print("Building N.I.N.A. SQLite database in memory...")
    conn = sqlite3.connect(":memory:")
    schema_sql = urllib.request.urlopen(NINA_BASE_URL + "Initial/initial_schema.sql", timeout=60).read().decode("utf-8", errors="replace")
    conn.executescript(schema_sql)

    data_sql = urllib.request.urlopen(NINA_BASE_URL + "Initial/initial_data.sql", timeout=60).read().decode("utf-8", errors="replace")
    conn.executescript(data_sql)

    for m in NINA_MIGRATIONS:
        url = f"{NINA_BASE_URL}Migration/{m}.sql"
        sql = urllib.request.urlopen(url, timeout=60).read().decode("utf-8", errors="replace")
        conn.executescript(sql)

    return conn


def format_catalog_designation(cat: str, desig: str) -> Optional[str]:
    """Format a (catalogue, designation) pair from NINA into clean display form."""
    c = str(cat).strip()
    d = str(desig).strip()
    if not d:
        return None
    cu = c.upper()
    if cu == "M":
        # Exclude Minkowski planetary nebulae (M 1-1, M 1-65, M 2-9, etc.) from Messier!
        if not re.match(r"^\d+$", d):
            return f"Minkowski {d}"
        num = int(d)
        if 1 <= num <= 110:
            return f"M {num}"
        return None
    if cu == "CALDWELL":
        return f"C {d}"
    if cu == "NGC":
        return f"NGC {d}"
    if cu == "IC":
        return f"IC {d}"
    if cu in ("SH2", "SHARPLESS"):
        return f"Sh2-{d}"
    if cu in ("BARNARD", "B"):
        return f"B {d}"
    if cu == "LBN":
        return f"LBN {d}"
    if cu == "LDN":
        return f"LDN {d}"
    if cu in ("VDB", "VDB-HA", "VDBH"):
        if cu == "VDB":
            return f"vdB {d}"
        return f"{c} {d}"
    if cu == "COLLINDER":
        return f"Cr {d}"
    if cu == "BERKELEY":
        return f"Berk {d}"
    if cu == "RUPRECHT":
        return f"Ru {d}"
    if cu == "ABELL":
        return f"Abell {d}"
    if cu == "PK":
        return f"PK {re.sub(r'\\s+', '', d)}"
    if cu == "UGC":
        return f"UGC {d}"
    if cu == "SNR":
        return f"SNR {d}"
    if cu == "HICKSON":
        return f"HCG {d}"
    if cu == "MELOTTE":
        return f"Mel {d}"
    if cu == "TRUMPLER":
        return f"Tr {d}"
    if cu == "NAME":
        return None
    return f"{c} {d}"


def generate_catalogs():
    conn = build_nina_sqlite()
    stellarium_names = fetch_stellarium_names()
    openngc_names = fetch_openngc_names()

    # Also read existing CSVs so we never lose existing names or Chandra SNR rows
    existing_names: Dict[str, str] = {}
    existing_chandra_df = None
    chandra_path = os.path.join(CATALOG_DIR, "chandra_snr.csv")
    if os.path.exists(chandra_path):
        existing_chandra_df = pd.read_csv(chandra_path)

    for csv_file in glob.glob(os.path.join(CATALOG_DIR, "*.csv")):
        try:
            df_old = pd.read_csv(csv_file)
            for _, r in df_old.iterrows():
                nt = normalize_token(r.get("designation", ""))
                nm = str(r.get("name", "")).strip()
                if nt and nm and nm not in ("N/A", "nan", "-"):
                    existing_names[nt] = nm
        except Exception:
            pass

    cur = conn.cursor()

    # Load all DsoDetail rows (excluding NONEX non-existent objects)
    dso_rows = cur.execute(
        """
        SELECT id, ra, dec, magnitude, surfacebrightness, sizemin, sizemax,
               constellation, dsotype, dsoclass
        FROM dsodetail
        WHERE dsotype IS NULL OR dsotype != 'NONEX'
        """
    ).fetchall()

    # Load all CatalogueNr rows grouped by dsodetailid
    cat_rows = cur.execute(
        "SELECT dsodetailid, catalogue, designation FROM cataloguenr"
    ).fetchall()

    dso_cats: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for dso_id, cat, desig in cat_rows:
        dso_cats[dso_id].append((str(cat).strip(), str(desig).strip()))

    # Target catalog buckets
    catalog_buckets: Dict[str, List[dict]] = defaultdict(list)

    # Map NINA catalogue name -> our target CSV filename (without .csv)
    PRIMARY_CAT_MAP = {
        "M": "messier",
        "Caldwell": "caldwell",
        "NGC": "ngc",
        "IC": "ic",
        "Sh2": "sharpless",
        "Barnard": "barnard",
        "LBN": "lbn",
        "LDN": "ldn",
        "vdB": "vdb",
        "Collinder": "collinder",
        "Abell": "abell",
        "PK": "pk",
        "UGC": "ugc",
        "Berkeley": "berkeley",
        "Ruprecht": "ruprecht",
    }

    # Notable astrophotography / deep-sky sub-catalogs to include in 'others.csv'
    OTHERS_ALLOW_CATS = {
        "Arp", "Hickson", "RCW", "Gum", "Melotte", "Trumpler", "Stock",
        "Ced", "Ou", "StDr", "vdB-Ha", "vdBH", "Sharpless", "Sh1",
        "Simeis", "King", "Dolidze", "Basel", "Bochum", "Biur", "Haffner",
        "Pismis", "Czernik", "Roslund", "Tombaugh", "Pal", "Ter", "Maffei",
        "Markarian", "Mrk", "3C", "AND", "DDO", "Kemble", "Coalsack", "Hyades",
        "Bernes", "Bark", "Antalova", "Auner", "Archinal", "Av-Hunter",
    }

    for dso in dso_rows:
        dso_id, ra, dec, mag, sb, sizemin, sizemax, const, dsotype, dsoclass = dso
        if ra is None or dec is None:
            continue

        mapped_type = NINA_TYPE_MAP.get(str(dsotype or "").strip().upper(), "Brt Neb")
        const_str = str(const).strip().upper() if const else "N/A"
        class_str = str(dsoclass).strip() if dsoclass and str(dsoclass).strip() else "N/A"
        mag_str = format_mag(mag)
        sb_str = format_mag(sb)
        size_str = format_size_arcmin(sizemax)
        ra_deg = float(ra)
        dec_deg = float(dec)
        ra_str = format_ra_string(ra_deg)
        dec_str = format_dec_string(dec_deg)

        entries = dso_cats.get(dso_id, [])

        # Collect all formatted designations and all NINA NAME entries for this DSO
        formatted_desigs: List[Tuple[str, str]] = []  # (cat_key, display_desig)
        nina_common_names: List[str] = []

        for cat, desig in entries:
            if cat.upper() == "NAME":
                if is_good_common_name(desig) and desig not in nina_common_names:
                    nina_common_names.append(desig.strip())
                continue
            disp = format_catalog_designation(cat, desig)
            if not disp:
                continue
            if cat == "M" and disp.startswith("Minkowski "):
                formatted_desigs.append(("Minkowski", disp))
            else:
                formatted_desigs.append((cat, disp))

        # Ensure dso_id itself is represented if no standard designation matched
        all_norm_tokens: Set[str] = set()
        for _, disp in formatted_desigs:
            nt = normalize_token(disp)
            if nt:
                all_norm_tokens.add(nt)
        dso_id_norm = normalize_token(dso_id)
        if dso_id_norm:
            all_norm_tokens.add(dso_id_norm)

        # Gather common names from Curated, Stellarium, OpenNGC, NINA, and existing CSVs
        candidate_names: List[str] = []
        # 1. Curated iconic names first
        for nt in sorted(all_norm_tokens):
            if nt in CURATED_COMMON_NAMES:
                cn = CURATED_COMMON_NAMES[nt]
                if cn not in candidate_names:
                    candidate_names.append(cn)

        # 2. Stellarium names
        for nt in sorted(all_norm_tokens):
            for cn in stellarium_names.get(nt, []):
                if is_good_common_name(cn) and cn not in candidate_names:
                    candidate_names.append(cn)

        # 3. OpenNGC names
        for nt in sorted(all_norm_tokens):
            for cn in openngc_names.get(nt, []):
                if is_good_common_name(cn) and cn not in candidate_names:
                    candidate_names.append(cn)

        # 4. NINA NAME entries (sorted by quality score)
        for cn in sorted(nina_common_names, key=score_common_name):
            if cn not in candidate_names:
                candidate_names.append(cn)

        # 5. Existing local CSV names
        for nt in sorted(all_norm_tokens):
            if nt in existing_names:
                cn = existing_names[nt]
                if cn not in candidate_names:
                    candidate_names.append(cn)

        primary_name = candidate_names[0].strip() if candidate_names else "N/A"
        extra_common_names = [
            cn.strip() for cn in candidate_names[1:]
            if normalize_token(cn) != normalize_token(primary_name)
        ]

        # All cross-catalog designations on this DSO
        all_disp_desigs = []
        for _, disp in formatted_desigs:
            if disp not in all_disp_desigs:
                all_disp_desigs.append(disp)

        # Determine which target catalogs this DSO belongs to
        placed_in_primary = False
        for cat, disp in formatted_desigs:
            if cat in PRIMARY_CAT_MAP:
                target_csv = PRIMARY_CAT_MAP[cat]
                placed_in_primary = True
                other_parts = [d for d in all_disp_desigs if normalize_token(d) != normalize_token(disp)]
                for ecn in extra_common_names:
                    if ecn not in other_parts:
                        other_parts.append(ecn)
                other_id_str = ";".join(other_parts[:12]) if other_parts else "N/A"

                catalog_buckets[target_csv].append({
                    "designation": disp,
                    "name": primary_name,
                    "other_id": other_id_str,
                    "type": mapped_type,
                    "constellation": const_str,
                    "magnitude": mag_str,
                    "surface_brightness": sb_str,
                    "size": size_str,
                    "ra_string": ra_str,
                    "dec_string": dec_str,
                    "ra_deg": ra_deg,
                    "dec_deg": dec_deg,
                    "class": class_str,
                })

        # Check if it should also go into 'others.csv' (or if it has a common name and isn't in a primary catalog)
        others_desig = None
        for cat, disp in formatted_desigs:
            if cat in OTHERS_ALLOW_CATS or cat == "Minkowski":
                others_desig = disp
                break
        if not others_desig and not placed_in_primary and primary_name != "N/A":
            others_desig = all_disp_desigs[0] if all_disp_desigs else dso_id

        if others_desig and not placed_in_primary:
            other_parts = [d for d in all_disp_desigs if normalize_token(d) != normalize_token(others_desig)]
            for ecn in extra_common_names:
                if ecn not in other_parts:
                    other_parts.append(ecn)
            other_id_str = ";".join(other_parts[:12]) if other_parts else "N/A"
            catalog_buckets["others"].append({
                "designation": others_desig,
                "name": primary_name,
                "other_id": other_id_str,
                "type": mapped_type,
                "constellation": const_str,
                "magnitude": mag_str,
                "surface_brightness": sb_str,
                "size": size_str,
                "ra_string": ra_str,
                "dec_string": dec_str,
                "ra_deg": ra_deg,
                "dec_deg": dec_deg,
                "class": class_str,
            })

    # Write each catalog bucket to CSV, deduplicating by normalized designation
    columns = [
        "designation", "name", "other_id", "type", "constellation",
        "magnitude", "surface_brightness", "size", "ra_string", "dec_string",
        "ra_deg", "dec_deg", "class"
    ]

    def natural_sort_key(desig: str):
        m = re.search(r"(\d+)", str(desig))
        return (int(m.group(1)) if m else 999999, str(desig))

    print("\nWriting enriched catalog CSVs:")
    for cat_name, rows in sorted(catalog_buckets.items()):
        df = pd.DataFrame(rows, columns=columns)
        # Deduplicate by normalized designation, preferring rows with common names and sizes
        df["_norm"] = df["designation"].map(normalize_token)
        df["_has_name"] = df["name"].map(lambda x: 0 if x in ("N/A", "", "-") else 1)
        df["_has_size"] = df["size"].map(lambda x: 0 if x in ("N/A", "", "-") else 1)
        df = df.sort_values(by=["_has_name", "_has_size"], ascending=[False, False])
        df = df.drop_duplicates(subset=["_norm"], keep="first")

        # Sort naturally by catalog number
        df["_sort"] = df["designation"].map(natural_sort_key)
        df = df.sort_values(by="_sort").drop(columns=["_norm", "_has_name", "_has_size", "_sort"]).reset_index(drop=True)

        out_path = os.path.join(CATALOG_DIR, f"{cat_name}.csv")
        df.to_csv(out_path, index=False)
        named_count = len(df[df["name"].notna() & (df["name"] != "N/A")])
        print(f"  -> {cat_name}.csv: {len(df)} objects ({named_count} with primary common name)")

    if existing_chandra_df is not None:
        named_c = len(existing_chandra_df[existing_chandra_df["name"].notna() & (existing_chandra_df["name"] != "N/A")])
        print(f"  -> chandra_snr.csv (preserved): {len(existing_chandra_df)} objects ({named_c} with primary common name)")


if __name__ == "__main__":
    generate_catalogs()
