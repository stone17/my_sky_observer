import pandas as pd
import os
import sys
from typing import Dict, List
import numpy as np

class CatalogManager:
    """Handles loading and caching of astronomical catalog data."""
    _cache: Dict[str, pd.DataFrame] = {}
    
    # FIX: Check for PyInstaller temp folder (_MEIPASS)
    if hasattr(sys, '_MEIPASS'):
        _catalog_path = os.path.join(sys._MEIPASS, 'catalogs')
    else:
        _catalog_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'catalogs')

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

    @classmethod
    def get_all_objects(cls, catalog_names: List[str]) -> pd.DataFrame:
        """
        Merges multiple catalogs into a single DataFrame.
        """
        print("\n--- Merging all requested catalogs ---")
        all_dfs = []
        for name in catalog_names:
            print(f"-> Processing catalog: '{name}'")
            try:
                df = cls.get_catalog(name)
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
        final_df = pd.concat(all_dfs, ignore_index=True)
        print(f"-> Concatenation complete. Total objects: {len(final_df)}")
        print("--- Finished merging catalogs ---\n")
            
        return final_df

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