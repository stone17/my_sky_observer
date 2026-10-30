import os
import sys
import warnings
import subprocess
from concurrent.futures import ThreadPoolExecutor
import PyInstaller.__main__
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

# Increase recursion limit for complex library analysis
sys.setrecursionlimit(5000)

def patch_frontend_dist():
    print("Patching frontend/dist/index.html...")
    dist_index = os.path.join("frontend", "dist", "index.html")
    if not os.path.exists(dist_index): return

    try:
        with open(dist_index, "r", encoding="utf-8") as f: content = f.read()
        modified = False
        style_tag = "<style>html, body { background-color: #111827; margin: 0; padding: 0; height: 100%; overflow: hidden; }</style>"
        
        if "background-color: #111827" not in content:
            content = content.replace("</head>", f"{style_tag}</head>")
            modified = True
        
        if "data-theme=\"dark\"" not in content and "<html" in content:
            content = content.replace("<html", '<html data-theme="dark"')
            modified = True
            
        if modified:
            with open(dist_index, "w", encoding="utf-8") as f: f.write(content)
            print("Patch successful.")
    except Exception as e: print(f"Patch failed: {e}")

def build_frontend():
    print("Building Frontend...")
    frontend_dir = os.path.join(os.getcwd(), 'frontend')
    if not os.path.exists(os.path.join(frontend_dir, 'node_modules')):
        subprocess.check_call(['npm', 'ci', '--ignore-scripts'], cwd=frontend_dir, shell=True)
    subprocess.check_call(['npm', 'run', 'build'], cwd=frontend_dir, shell=True)
    patch_frontend_dist()
    print("Frontend build complete.")

def collect_package_assets(pkg):
    pkg_datas = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            pkg_datas.extend(collect_data_files(pkg))
        except Exception as e:
            print(f"Warning: Could not collect data files for {pkg}: {e}")
        try:
            pkg_datas.extend(copy_metadata(pkg))
        except Exception:
            pass
    return pkg_datas

def prepare_pyinstaller_datas():
    datas = [
        ('frontend/dist', 'frontend/dist'),
        ('catalogs', 'catalogs'),
        ('settings_default.yaml', '.'),
        ('components.yaml', '.')
    ]
    packages = ['astropy', 'astropy_iers_data', 'astroplan']
    with ThreadPoolExecutor(max_workers=len(packages)) as executor:
        for pkg_datas in executor.map(collect_package_assets, packages):
            datas.extend(pkg_datas)
    return datas

def build_exe(datas):
    print("Building Executable...")
    
    # Targeted hidden imports actually used by the app
    hidden_imports = [
        'uvicorn', 'uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.auto', 
        'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto', 
        'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on',
        'webview', 'webview.platforms.winforms', 
        'yaml', 'astropy_iers_data'
    ]

    # Exclude unused heavy/optional packages and database drivers to speed up analysis and silence warnings
    excluded_modules = [
        'numba',
        'astropy.samp',
        'scipy',
        'sklearn',
        'matplotlib',
        'tkinter',
        'pytest',
        'IPython',
        'notebook',
        'pyvo',
        'sqlalchemy',
        'MySQLdb',
        'pysqlite2',
        'psycopg2',
        'pyarrow',
        'tables',
        'openpyxl',
        'xlrd',
        'xlsxwriter',
        'odf',
        'pyxlsb',
        'pycparser.lextab',
        'pycparser.yacctab',
    ]

    entry_script = 'run.py' if os.path.exists('run.py') else 'desktop_app.py'
    
    args = [
        entry_script,
        '--name=MySkyObserver',
        '--onefile',
        '--console', # Keep console enabled for debugging if it crashes
        '--icon=NONE',
    ]

    if '--clean' in sys.argv:
        args.append('--clean')
    
    separator = ';' if os.name == 'nt' else ':'
    
    for src, dst in datas:
        args.append(f'--add-data={src}{separator}{dst}')

    for module in hidden_imports:
        args.append(f'--hidden-import={module}')

    for module in excluded_modules:
        args.append(f'--exclude-module={module}')
        
    PyInstaller.__main__.run(args)
    print("Build Complete! Check 'dist/MySkyObserver.exe'")

if __name__ == '__main__':
    try:
        # Run frontend build and PyInstaller asset collection in parallel
        with ThreadPoolExecutor(max_workers=2) as executor:
            frontend_future = executor.submit(build_frontend)
            datas_future = executor.submit(prepare_pyinstaller_datas)
            frontend_future.result()
            datas = datas_future.result()

        build_exe(datas)
    except Exception as e:
        print(f"Build Failed: {e}")
