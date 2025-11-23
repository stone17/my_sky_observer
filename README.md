# My Sky Observer

A web-based astronomy target framing and planning tool.

## Features
- **Target Planning**: Filter and sort targets based on altitude, visibility, and type.
- **Framing**: Visualize your telescope and camera's field of view on deep sky survey images.
- **N.I.N.A Integration**: Send target coordinates and rotation directly to N.I.N.A.
- **Offline Caching**: Caches images for offline use.

## Installation & Setup

### Prerequisites
- Python 3.10+
- Node.js & npm (for building the frontend)

### Backend Setup
1.  Create a virtual environment (optional but recommended):
    ```bash
    python -m venv venv
    source venv/bin/activate  # Linux/Mac
    venv\Scripts\activate     # Windows
    ```
2.  Install dependencies:
    ```bash
    pip install -r requirements.txt
    ```
    *Note: If you want to run tests, ensure `pytest` and `pytest-asyncio` are installed.*

### Frontend Setup
The frontend is built using Vue 3 and Vite.

1.  Navigate to the frontend directory:
    ```bash
    cd frontend
    ```
2.  Install dependencies:
    ```bash
    npm install
    ```
3.  Build the frontend:
    ```bash
    npm run build
    ```
    This generates the production files in `frontend/dist`.

### Running the Application
To start the application (backend + frontend):

```bash
python run.py
```
This will start the server and attempt to open a window (using `pywebview`) or you can access it in your browser at `http://127.0.0.1:8000`.

## Development
- **Frontend Dev Server**: Run `npm run dev` in the `frontend` folder for hot-reloading during UI development.
- **Backend Dev**: Run `uvicorn backend.main:app --reload` to auto-restart on code changes.

## Testing
Run the backend tests:
```bash
pytest
```
