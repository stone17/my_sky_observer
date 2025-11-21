import uvicorn
import threading
import webview
import sys
import time
from urllib.request import urlopen

# Import your FastAPI app instance
from backend.main import app

def run_server():
    """Runs the Uvicorn server."""
    uvicorn.run(app, host="127.0.0.1", port=8000)

def wait_for_server(url: str, timeout: int = 10):
    """Waits for the server to be ready before opening the window."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            # Try to open the URL; if it works, the server is up
            urlopen(url)
            print("Server is up!")
            return True
        except Exception:
            # Server is not ready yet, wait a bit
            time.sleep(0.1)
    print("Error: Server did not start within the timeout period.")
    return False

if __name__ == '__main__':
    # Run the FastAPI server in a separate thread
    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()

    # Wait for the server to be ready
    if not wait_for_server("http://127.0.0.1:8000"):
        sys.exit(1) # Exit if the server fails to start

    # Create and start the pywebview window
    try:
        webview.create_window(
            'Astro Framing Assistant',
            'http://127.0.0.1:8000',
            width=1400,
            height=900,
            resizable=True
        )
        # Enable debugging to open the web inspector
        webview.start(debug=True)
    except Exception as e:
        print(f"Failed to create webview window: {e}")
    finally:
        sys.exit()