import pandas as pd
import os
from typing import Dict, List

class CatalogManager:
    """Handles loading and caching of astronomical catalog data."""
    _cache: Dict[str, pd.DataFrame] = {}
    _catalog_path = os.path.join(os.path.dirname(__file__), 'catalogs')

    @classmethod
    def get_catalog(cls, catalog_name: str) -> pd.DataFrame:
        """
        Retrieves a catalog DataFrame, loading it from a CSV file if not cached.
        """
        print(f"--- Attempting to get catalog: {catalog_name} ---")
        if catalog_name not in cls._cache:
            file_path = os.path.join(cls._catalog_path, f"{catalog_name}.csv")
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"Catalog file not found: {file_path}")

            print(f"  -> Loading '{catalog_name}' catalog from {file_path}...")
            df = pd.read_csv(file_path)
            print(f"  -> Successfully read CSV. Columns found: {list(df.columns)}")
            print(f"  -> First 5 rows of data:\n{df.head().to_string()}")

            # Basic validation
            required_cols = ['id', 'ra', 'dec', 'maj_ax']
            print(f"  -> Validating required columns: {required_cols}...")
            if not all(col in df.columns for col in required_cols):
                raise ValueError(f"Catalog '{catalog_name}' is missing one or more required columns.")
            print("  -> Validation successful.")

            cls._cache[catalog_name] = df
            print(f"  -> Catalog '{catalog_name}' cached successfully with {len(df)} objects.")

        else:
            print(f"  -> Catalog '{catalog_name}' found in cache.")

        return cls._cache[catalog_name]

    @classmethod
    def get_all_objects(cls, catalog_names: List[str]) -> pd.DataFrame:
        """
        Merges multiple catalogs into a single DataFrame, skipping any that are not found or malformed.
        """
        print("\n--- Merging all requested catalogs ---")
        all_dfs = []
        for name in catalog_names:
            print(f"-> Processing catalog: '{name}'")
            try:
                df = cls.get_catalog(name)
                df['catalog'] = name.upper()
                all_dfs.append(df)
                print(f"  -> SUCCESS: Added {len(df)} objects from '{name}' to the processing list.")
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
        print(f"-> Concatenation complete. Total objects from all catalogs: {len(final_df)}")
        print("--- Finished merging catalogs ---\n")
            
        return final_df