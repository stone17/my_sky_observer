import os
import subprocess
import shutil
import PyInstaller.__main__

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
    # Format: "source;dest" (Windows)
    datas = [
        ('frontend/dist', 'frontend/dist'),
        ('settings_default.yaml', '.'),
        ('components.yaml', '.'),
        ('catalogs', 'catalogs'),
    ]
    
    # Hidden imports often needed
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
        'webview.platforms.winforms' 
    ]
    
    args = [
        'run.py',
        '--name=MySkyObserver',
        '--onefile',
        '--clean',
        '--windowed', # No console window
        '--icon=NONE' # default icon
    ]
    
    for src, dst in datas:
        args.append(f'--add-data={src}{os.pathsep}{dst}')
        
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
