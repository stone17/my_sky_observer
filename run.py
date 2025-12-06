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
    # Run uvicorn without capturing signals to prevent it from fighting with C# or webview
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
    # Run the FastAPI server in a separate thread
    server_thread = threading.Thread(target=run_server)
    server_thread.daemon = True
    server_thread.start()

    # Wait for the server to be ready
    if not wait_for_server("http://127.0.0.1:8000"):
        print("Server failed to start. Exiting.")
        return  # CHANGED: Use return instead of sys.exit(1)

    # Create and start the pywebview window
    try:
        webview.create_window(
            'Astro Framing Assistant',
            'http://127.0.0.1:8000',
            width=1400,
            height=900,
            resizable=True
        )
        # Blocks here until window is closed
        webview.start(debug=True)
    except Exception as e:
        print(f"Failed to create webview window: {e}")
    finally:
        # CHANGED: Removed sys.exit()
        print("Application finished.")

if __name__ == '__main__':
    main()