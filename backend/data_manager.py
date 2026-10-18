import pandas as pd
import os
import re
import sys
from typing import Dict, List, Tuple
import numpy as np

CATALOG_PRIORITY = {
    'MESSIER': 1,
    'NGC': 2,
    'IC': 3,
    'SHARPLESS': 4,
    'COLLINDER': 5,
    'BARNARD': 6,
    'ABELL': 7,
    'UGC': 8,
    'BERKELEY': 9,
    'PK': 10,
    'RUPRECHT': 11,
    'CHANDRA_SNR': 12,
    'OTHERS': 13,
}


class CatalogManager:
    """Handles loading and caching of astronomical catalog data."""
    _cache: Dict[str, pd.DataFrame] = {}
    _merged_cache: Dict[Tuple[str, ...], pd.DataFrame] = {}
    
    # FIX: Check for PyInstaller temp folder (_MEIPASS)
    if hasattr(sys, '_MEIPASS'):
        _catalog_path = os.path.join(sys._MEIPASS, 'catalogs')
    else:
        _catalog_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'catalogs')

    @staticmethod
    def normalize_designation(val) -> str:
        """Normalize a catalog designation or name token for canonical matching."""
        if pd.isna(val):
            return ""
        s = str(val).strip()
        if not s or s.lower() in ('n/a', 'nan'):
            return ""
        s = re.sub(r'\s+', ' ', s).upper()
        s = re.sub(r'^COLLINDER\s+', 'CR ', s)
        s = re.sub(r'^BERKELEY\s+', 'BERK ', s)
        s = re.sub(r'^RUPRECHT\s+', 'RU ', s)
        s = re.sub(r'^SH\s*2[\-\s]+', 'SH2-', s)
        return s

    @staticmethod
    def clean_display_name(val) -> str:
        """Clean redundant internal whitespace for display while preserving original casing."""
        if pd.isna(val):
            return ""
        s = str(val).strip()
        if not s or s.lower() in ('n/a', 'nan'):
            return ""
        return re.sub(r'\s+', ' ', s)

    @staticmethod
    def parse_size(size_str):
        """Parses size string (e.g., "8'", "480''") to arcminutes (float)."""
        if pd.isna(size_str) or size_str == 'N/A' or not isinstance(size_str, str):
            return 0.0
        
        size_str = size_str.strip()
        try:
            if size_str.endswith("''"):
                return float(size_str[:-2]) / 60.0
            elif size_str.endswith("'"):
                return float(size_str[:-1])
            else:
                # Assume arcminutes if no suffix, or try to parse
                return float(size_str)
        except ValueError:
            return 0.0

    @classmethod
    def get_catalog(cls, catalog_name: str) -> pd.DataFrame:
        """
        Retrieves a catalog DataFrame, loading it from a CSV file if not cached.
        Expects the NEW catalog format:
        designation,name,other_id,type,constellation,magnitude,surface_brightness,size,ra_string,dec_string,ra_deg,dec_deg,class
        """
        print(f"--- Attempting to get catalog: {catalog_name} ---")
        if catalog_name not in cls._cache:
            file_path = os.path.join(cls._catalog_path, f"{catalog_name}.csv")
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"Catalog file not found: {file_path}")

            print(f"  -> Loading '{catalog_name}' catalog from {file_path}...")
            try:
                df = pd.read_csv(file_path)
            except Exception as e:
                raise ValueError(f"Failed to read CSV for {catalog_name}: {e}")

            # Check if it's the new format
            if 'designation' in df.columns and 'ra_deg' in df.columns:
                print(f"  -> Detected NEW format for {catalog_name}.")
                
                # Rename columns to internal standard
                df = df.rename(columns={
                    'designation': 'id',
                    'ra_deg': 'ra',
                    'dec_deg': 'dec',
                    'magnitude': 'mag',
                    'size': 'maj_ax'
                })

                # Parse size column to numeric (arcminutes)
                if 'maj_ax' in df.columns:
                    df['maj_ax'] = df['maj_ax'].apply(cls.parse_size)

                # Fill N/A names
                df['name'] = df['name'].fillna('N/A')
                
                # Keep relevant columns
                keep_cols = ['id', 'name', 'other_id', 'type', 'constellation', 'ra', 'dec', 'mag', 'maj_ax', 'surface_brightness']
                # Filter to only existing columns (surface_brightness might be missing in some future files?)
                keep_cols = [c for c in keep_cols if c in df.columns]
                df = df[keep_cols]
                
            else:
                print(f"  -> WARNING: Catalog {catalog_name} does not match the NEW format (missing 'designation' or 'ra_deg').")
                print(f"  -> Columns found: {list(df.columns)}")
                raise ValueError(f"Catalog {catalog_name} is in an unsupported format.")

            cls._cache[catalog_name] = df
            print(f"  -> Catalog '{catalog_name}' cached successfully with {len(df)} objects.")

        else:
            print(f"  -> Catalog '{catalog_name}' found in cache.")

        return cls._cache[catalog_name]

    @staticmethod
    def _alias_sort_key(alias_str: str) -> Tuple[int, str]:
        """Sort aliases so well-known catalog designations appear first."""
        u = alias_str.upper()
        if re.match(r'^M\s+\d+', u):
            return (1, u)
        if re.match(r'^NGC\s+\d+', u):
            return (2, u)
        if re.match(r'^IC\s+\d+', u):
            return (3, u)
        if re.match(r'^SH2[\-\s]\d+', u):
            return (4, u)
        if re.match(r'^(CR|COLLINDER)\s+\d+', u):
            return (5, u)
        if re.match(r'^B\s*\d+', u):
            return (6, u)
        if re.match(r'^ABELL\s+\d+', u):
            return (7, u)
        if re.match(r'^UGC\s+\d+', u):
            return (8, u)
        return (9, u)

    @classmethod
    def get_all_objects(cls, catalog_names: List[str]) -> pd.DataFrame:
        """
        Merges multiple catalogs into a single DataFrame and collates objects that
        represent the same astronomical target or tightly grouped sub-components.
        """
        cache_key = tuple(sorted(str(c).strip().lower() for c in catalog_names if str(c).strip()))
        if not cache_key:
            return pd.DataFrame()
        if cache_key in cls._merged_cache:
            return cls._merged_cache[cache_key].copy()

        print("\n--- Merging all requested catalogs ---")
        all_dfs = []
        for name in catalog_names:
            print(f"-> Processing catalog: '{name}'")
            try:
                df = cls.get_catalog(name).copy()
                df['catalog'] = name.upper()
                all_dfs.append(df)
                print(f"  -> SUCCESS: Added {len(df)} objects from '{name}'.")
            except (FileNotFoundError, ValueError) as e:
                print(f"  -> WARNING: Could not load catalog '{name}'. Reason: {e}. Skipping.")
                continue
            except Exception as e:
                print(f"  -> UNEXPECTED ERROR while loading '{name}': {e}. Skipping.")
                continue

        if not all_dfs:
            print("-> No dataframes were loaded. Returning empty dataframe.")
            return pd.DataFrame()

        print(f"-> Preparing to concatenate {len(all_dfs)} dataframes.")
        raw = pd.concat(all_dfs, ignore_index=True)

        # Clean display IDs and rank candidates so the best representative is always at the root
        raw['id'] = raw['id'].apply(cls.clean_display_name)
        raw['cat_rank'] = raw['catalog'].map(lambda c: CATALOG_PRIORITY.get(str(c).upper(), 99))
        raw['has_common'] = raw['name'].map(
            lambda n: 0 if (pd.isna(n) or str(n).strip() in ('', 'N/A', 'nan')) else 1
        )
        raw['maj_ax'] = pd.to_numeric(raw['maj_ax'], errors='coerce').fillna(0.0)
        raw['mag'] = pd.to_numeric(raw['mag'], errors='coerce')
        raw['maj_num'] = raw['maj_ax']
        raw['mag_num'] = raw['mag'].fillna(99.0)

        raw = raw.sort_values(
            by=['has_common', 'cat_rank', 'maj_num', 'mag_num', 'id'],
            ascending=[False, True, False, True, True]
        ).reset_index(drop=True)

        n = len(raw)
        parent = list(range(n))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        ras_deg = pd.to_numeric(raw['ra'], errors='coerce').fillna(0.0).values
        decs_deg = pd.to_numeric(raw['dec'], errors='coerce').fillna(0.0).values
        ras_rad = np.radians(ras_deg)
        decs_rad = np.radians(decs_deg)
        cos_dec = np.cos(decs_rad)
        maj_arr = raw['maj_num'].values
        norm_ids = [cls.normalize_designation(v) for v in raw['id'].values]
        is_messier = [bool(re.match(r'^M\s+\d+$', nid)) for nid in norm_ids]

        def ang_dist_arcmin(i: int, j: int) -> float:
            dra = (ras_rad[i] - ras_rad[j]) * 0.5 * (cos_dec[i] + cos_dec[j])
            ddec = decs_rad[i] - decs_rad[j]
            return float(np.degrees(np.hypot(dra, ddec)) * 60.0)

        def can_merge_roots(ri: int, rj: int) -> bool:
            if ri == rj:
                return False
            # Never merge two distinct Messier catalog objects (e.g. M 42 and M 43)
            if is_messier[ri] and is_messier[rj] and norm_ids[ri] != norm_ids[rj]:
                return False
            return True

        def union(i: int, j: int):
            ri, rj = find(i), find(j)
            if not can_merge_roots(ri, rj):
                return
            if ri < rj:
                parent[rj] = ri
            else:
                parent[ri] = rj

        # 1. Cross-catalog and intra-catalog linking via normalized ID & other_id tokens
        token_to_idx: Dict[str, int] = {}
        explicit_synonym_pairs = set()
        for i in range(n):
            tokens = []
            if norm_ids[i]:
                tokens.append(norm_ids[i])
            other_raw = raw.at[i, 'other_id']
            if pd.notna(other_raw):
                for part in re.split(r'[,;/]', str(other_raw)):
                    t = cls.normalize_designation(part)
                    if t:
                        tokens.append(t)
            for t in tokens:
                if t in token_to_idx:
                    j = token_to_idx[t]
                    max_syn_dist = max(20.0, float(max(maj_arr[i], maj_arr[j])) * 0.6)
                    if ang_dist_arcmin(i, j) <= max_syn_dist:
                        union(i, j)
                        explicit_synonym_pairs.add((min(i, j), max(i, j)))
                else:
                    token_to_idx[t] = i

        # 2. Coordinate proximity & embedded sub-object collation (anchored to cluster root `ri`)
        for i in range(n):
            ri = find(i)
            dra = (ras_rad[i + 1:] - ras_rad[ri]) * cos_dec[ri]
            ddec = decs_rad[i + 1:] - decs_rad[ri]
            dists = np.degrees(np.hypot(dra, ddec)) * 60.0
            radius_ri = max(6.0, min(float(maj_arr[ri]) * 0.45, 32.0))
            matches = np.where(dists <= radius_ri)[0] + i + 1
            for j in matches:
                rj = find(j)
                if not can_merge_roots(ri, rj):
                    continue
                radius_rj = max(6.0, min(float(maj_arr[rj]) * 0.45, 32.0))
                if ang_dist_arcmin(ri, rj) <= max(radius_ri, radius_rj):
                    union(ri, rj)
                    ri = find(ri)

        # 3. Build collated rows with aggregated aliases and distinct sub-objects
        groups: Dict[int, List[int]] = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(i)

        records = raw.to_dict('records')
        collated_rows = []

        for root_idx, member_idxs in groups.items():
            primary = dict(records[root_idx])
            primary_id = primary['id']
            primary_norm_id = norm_ids[root_idx]

            # Pick best common name in group if primary has N/A
            primary_common = cls.clean_display_name(primary.get('name', 'N/A'))
            if not primary_common:
                for idx in member_idxs:
                    cand = cls.clean_display_name(records[idx].get('name', 'N/A'))
                    if cand:
                        primary_common = cand
                        break
            primary['name'] = primary_common if primary_common else 'N/A'
            primary_norm_common = cls.normalize_designation(primary['name'])

            # Pick best size and magnitude if primary is missing them
            if (not primary.get('maj_ax') or primary.get('maj_ax', 0) <= 0):
                for idx in member_idxs:
                    if records[idx].get('maj_ax', 0) > 0:
                        primary['maj_ax'] = records[idx]['maj_ax']
                        break
            if pd.isna(primary.get('mag')) or float(primary.get('mag', 99) or 99) >= 99:
                for idx in member_idxs:
                    m_val = records[idx].get('mag')
                    if pd.notna(m_val) and float(m_val) < 99:
                        primary['mag'] = m_val
                        break

            # Collect deduplicated aliases (preserving nice display formatting)
            alias_map: Dict[str, str] = {}
            for idx in member_idxs:
                rec = records[idx]
                candidates = [rec.get('id'), rec.get('name')]
                other_val = rec.get('other_id')
                if pd.notna(other_val):
                    candidates.extend(re.split(r'[,;/]', str(other_val)))
                for c in candidates:
                    disp = cls.clean_display_name(c)
                    norm_c = cls.normalize_designation(disp)
                    if not disp or not norm_c:
                        continue
                    if norm_c in (primary_norm_id, primary_norm_common):
                        continue
                    if norm_c not in alias_map:
                        alias_map[norm_c] = disp

            sorted_aliases = sorted(alias_map.values(), key=cls._alias_sort_key)
            primary['aliases'] = ", ".join(sorted_aliases)

            # Collect distinct spatial sub-objects in the collated group (separated by > 0.35 arcmin)
            sub_objects = []
            placed_coords = [(float(primary['ra']), float(primary['dec']))]
            for idx in member_idxs:
                if idx == root_idx:
                    continue
                if (min(root_idx, idx), max(root_idx, idx)) in explicit_synonym_pairs:
                    continue
                rec = records[idx]
                if norm_ids[idx] == primary_norm_id:
                    continue
                r_ra = float(rec['ra'])
                r_dec = float(rec['dec'])
                # Check angular separation against primary and already placed sub-objects
                too_close = False
                for p_ra, p_dec in placed_coords:
                    dra = (r_ra - p_ra) * np.cos(np.radians(0.5 * (r_dec + p_dec)))
                    ddec = r_dec - p_dec
                    if np.hypot(dra, ddec) * 60.0 <= 0.35:
                        too_close = True
                        break
                if not too_close:
                    placed_coords.append((r_ra, r_dec))
                    sub_mag = rec.get('mag')
                    sub_objects.append({
                        'name': rec['id'],
                        'common_name': rec.get('name', 'N/A'),
                        'type': rec.get('type', ''),
                        'ra': round(r_ra, 5),
                        'dec': round(r_dec, 5),
                        'maj_ax': round(float(rec.get('maj_ax') or 0.0), 2),
                        'mag': round(float(sub_mag), 2) if pd.notna(sub_mag) else None,
                    })

            primary['sub_objects'] = sub_objects
            for temp_col in ('cat_rank', 'has_common', 'maj_num', 'mag_num'):
                primary.pop(temp_col, None)
            collated_rows.append(primary)

        final_df = pd.DataFrame(collated_rows)
        print(f"-> Concatenation & Smart Collation complete. Total unique objects: {len(final_df)}")
        print("--- Finished merging catalogs ---\n")

        cls._merged_cache[cache_key] = final_df
        return final_df.copy()

    @staticmethod
    def get_available_catalogs() -> List[str]:
        """
        Scans the catalogs directory and returns a list of available catalog names (without .csv extension).
        """
        catalogs = []
        if os.path.exists(CatalogManager._catalog_path):
            for filename in os.listdir(CatalogManager._catalog_path):
                if filename.endswith(".csv"):
                    catalogs.append(filename[:-4])
        return sorted(catalogs)
