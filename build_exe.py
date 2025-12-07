import os
import sys
import subprocess
import shutil
import PyInstaller.__main__

# FIX: Increase recursion limit for pandas/astropy
sys.setrecursionlimit(5000)

def patch_frontend_dist():
    """
    Applies the Dark Mode / Background fix to the fresh build
    BEFORE PyInstaller bundles it.
    """
    print("Patching frontend/dist/index.html for Dark Mode...")
    dist_index = os.path.join("frontend", "dist", "index.html")
    
    if not os.path.exists(dist_index):
        print(f"WARNING: {dist_index} not found. Skipping patch.")
        return

    try:
        with open(dist_index, "r", encoding="utf-8") as f:
            content = f.read()

        modified = False
        style_tag = "<style>html, body { background-color: #111827; margin: 0; padding: 0; height: 100%; overflow: hidden; }</style>"

        if "background-color: #111827" not in content:
            content = content.replace("</head>", f"{style_tag}</head>")
            modified = True

        if "data-theme=\"dark\"" not in content:
            if "<html" in content and "data-theme" not in content:
                content = content.replace("<html", '<html data-theme="dark"')
                modified = True

        if modified:
            with open(dist_index, "w", encoding="utf-8") as f:
                f.write(content)
            print("Patch successful.")
        else:
            print("File already patched.")
            
    except Exception as e:
        print(f"Patch failed: {e}")

def build_frontend():
    print("Building Frontend...")
    frontend_dir = os.path.join(os.getcwd(), 'frontend')
    
    if not os.path.exists(os.path.join(frontend_dir, 'node_modules')):
        subprocess.check_call(['npm', 'install'], cwd=frontend_dir, shell=True)
    
    subprocess.check_call(['npm', 'run', 'build'], cwd=frontend_dir, shell=True)
    
    dist_dir = os.path.join(frontend_dir, 'dist')
    if not os.path.exists(dist_dir):
        raise Exception("Frontend build failed: dist directory not found")
        
    # FIX: Apply the patch immediately after build
    patch_frontend_dist()
    print("Frontend build complete.")

def build_exe():
    print("Building Executable...")
    
    datas = [
        ('frontend/dist', 'frontend/dist'),
        ('catalogs', 'catalogs'),
        ('settings_default.yaml', '.'),
        ('components.yaml', '.')
    ]
    
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
        'scipy.special.cython_special',
        'sklearn.utils._typedefs'
    ]
    
    # Check for correct entry script
    entry_script = 'run.py' if os.path.exists('run.py') else 'desktop_app.py'
    
    args = [
        entry_script,
        '--name=MySkyObserver',
        '--onefile',
        '--clean',
        '--windowed',
        '--icon=NONE'
    ]
    
    separator = ';' if os.name == 'nt' else ':'
    
    for src, dst in datas:
        args.append(f'--add-data={src}{separator}{dst}')
        
    for module in hidden_imports:
        args.append(f'--hidden-import={module}')
        
    PyInstaller.__main__.run(args)
    print("Build Complete! Check 'dist/MySkyObserver.exe'")

if __name__ == '__main__':
    try:
        build_frontend()
        build_exe()
    except Exception as e:
        print(f"Build Failed: {e}")