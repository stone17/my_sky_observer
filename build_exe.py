import os
import sys # <--- ADDED: Needed to set recursion limit
import subprocess
import shutil
import PyInstaller.__main__

# FIX: Increase recursion limit to prevent PyInstaller crash with pandas/astropy
sys.setrecursionlimit(5000)

def build_frontend():
    print("Building Frontend...")
    frontend_dir = os.path.join(os.getcwd(), 'frontend')
    # Install dependencies if node_modules missing
    if not os.path.exists(os.path.join(frontend_dir, 'node_modules')):
        subprocess.check_call(['npm', 'install'], cwd=frontend_dir, shell=True)
    
    # Build
    subprocess.check_call(['npm', 'run', 'build'], cwd=frontend_dir, shell=True)
    
    # Verify dist
    dist_dir = os.path.join(frontend_dir, 'dist')
    if not os.path.exists(dist_dir):
        raise Exception("Frontend build failed: dist directory not found")
    print("Frontend build complete.")

def build_exe():
    print("Building Executable...")
    
    # Define data to bundle
    # Format: ('source_folder', 'dest_folder_internal')
    datas = [
        ('frontend/dist', 'frontend/dist'),
        ('catalogs', 'catalogs'),           # Ensure catalogs are included
        ('settings_default.yaml', '.'),
        ('components.yaml', '.')
    ]
    
    # Hidden imports often needed for Uvicorn/FastAPI/WebView
    hidden_imports = [
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan.on',
        'engineio.async_drivers.threading',
        'webview.platforms.winforms',
        # Scientific libs hooks sometimes fail to grab these
        'scipy.special.cython_special',
        'sklearn.utils._typedefs'
    ]
    
    # Note: Ensure 'desktop_app.py' matches your actual main entry file (e.g., run.py)
    entry_script = 'run.py' if os.path.exists('run.py') else 'desktop_app.py'
    
    args = [
        entry_script,
        '--name=MySkyObserver',
        '--onefile',
        '--clean',
        '--windowed', # No console window
        '--icon=NONE' # default icon
    ]
    
    # Handle OS-specific separator for binary data
    separator = ';' if os.name == 'nt' else ':'
    
    for src, dst in datas:
        args.append(f'--add-data={src}{separator}{dst}')
        
    for module in hidden_imports:
        args.append(f'--hidden-import={module}')
        
    PyInstaller.__main__.run(args)
    print("Build Complete! Check 'dist/MySkyObserver.exe'")

if __name__ == '__main__':
    try:
        # build_frontend() # Uncomment to rebuild frontend every time
        build_exe()
    except Exception as e:
        print(f"Build Failed: {e}")