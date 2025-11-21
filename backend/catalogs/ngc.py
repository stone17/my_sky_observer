import pandas as pd

def format_ngc_id(ngc_id):
    """
    Formats an NGC ID string like 'NGC 0001' to 'NGC1'.
    """
    # Remove any whitespace
    ngc_id_no_space = ngc_id.replace(' ', '')
    
    # Safely extract the numeric part
    if ngc_id_no_space.startswith('NGC'):
        numeric_part = ngc_id_no_space[3:]
        if numeric_part.isdigit():
            # Convert to integer to remove leading zeros, then back to string
            return 'NGC' + str(int(numeric_part))
            
    # Return original if not in the expected format
    return ngc_id

# --- Main Script ---

# 1. Load your existing ngc.csv file
try:
    df_user_ngc = pd.read_csv('ngc.csv')
except FileNotFoundError:
    df_user_ngc = pd.DataFrame()

# 2. Load the complete NGC catalog, specifying the semicolon separator
df_opennc = pd.read_csv('NGC_full.csv', sep=';')

# 3. Filter for NGC objects and rename columns
df_ngc_all = df_opennc[df_opennc['Name'].str.startswith('NGC', na=False)].copy()
df_ngc_all = df_ngc_all[['Name', 'RA', 'Dec', 'MajAx', 'MinAx']]
df_ngc_all.rename(columns={
    'Name': 'id',
    'RA': 'ra',
    'Dec': 'dec',
    'MajAx': 'maj_ax',
    'MinAx': 'min_ax'
}, inplace=True)

# 4. Apply the new function to clean and format the 'id' column
df_ngc_all['id'] = df_ngc_all['id'].apply(format_ngc_id)

# 5. Combine your data with the full catalog, ensuring no duplicates
df_completed_ngc = pd.concat([df_user_ngc, df_ngc_all]).drop_duplicates(subset=['id'], keep='first')

# 6. Save the final, complete catalog
df_completed_ngc.to_csv('ngc_completed.csv', index=False)

print("Successfully created ngc_completed.csv with standardized IDs!")