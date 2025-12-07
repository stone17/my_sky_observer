import uvicorn
import threading
import webview
import sys
import time
import os
from urllib.request import urlopen

# Import your FastAPI app instance
from backend.main import app

DEBUG = False

def patch_frontend_dist():
    """
    Patches the built HTML to force Dark Mode and correct background color.
    """
    dist_index = os.path.join("frontend", "dist", "index.html")
    
    if not os.path.exists(dist_index):
        print(f"WARNING: {dist_index} not found. Skipping patch.")
        return

    try:
        with open(dist_index, "r", encoding="utf-8") as f:
            content = f.read()

        modified = False

        # 1. Inject Style Block (Background Color)
        style_tag = "<style>html, body { background-color: #111827; margin: 0; padding: 0; height: 100%; overflow: hidden; }</style>"
        if "background-color: #111827" not in content:
            print("PATCHING: Injecting dark background style...")
            content = content.replace("</head>", f"{style_tag}</head>")
            modified = True

        # 2. Force PicoCSS Dark Mode (prevents white flash from framework)
        if "data-theme=\"dark\"" not in content:
            print("PATCHING: Forcing PicoCSS Dark Mode...")
            # Replace <html> or <html lang="en"> with <html data-theme="dark" lang="en">
            if "<html" in content and "data-theme" not in content:
                content = content.replace("<html", '<html data-theme="dark"')
                modified = True

        if modified:
            with open(dist_index, "w", encoding="utf-8") as f:
                f.write(content)
            print("PATCHING: Success. HTML updated.")
        else:
            print("PATCHING: File is already up to date.")
            
    except Exception as e:
        print(f"PATCHING FAILED: {e}")

def run_server():
    """Runs the Uvicorn server."""
    uvicorn.run(app, host="127.0.0.1", port=8000)

def wait_for_server(url: str, timeout: int = 10):
    """Waits for the server to be ready before opening the window."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            urlopen(url)
            print("Server is up!")
            return True
        except Exception:
            time.sleep(0.1)
    print("Error: Server did not start within the timeout period.")
    return False

def main():
    # 1. PATCH HTML
    patch_frontend_dist()

    # 2. Start Server
    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()

    # 3. Wait for Server
    if not wait_for_server("http://127.0.0.1:8000"):
        print("Server failed to start. Exiting.")
        return 

    # 4. Cache Busting URL
    timestamp = int(time.time())
    entry_url = f'http://127.0.0.1:8000/?t={timestamp}'

    # 5. Launch Window
    try:
        webview.create_window(
            'Astro Framing Assistant',
            entry_url,
            width=1400,
            height=900,
            resizable=True,
            background_color='#111827' 
        )
        webview.start(debug=DEBUG)
    except Exception as e:
        print(f"Failed to create webview window: {e}")
    finally:
        print("Application finished.")

if __name__ == '__main__':
    main()