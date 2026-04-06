# Stage 1: Build the Vue.js Frontend
FROM node:20-alpine AS frontend-builder
WORKDIR /app/frontend

COPY frontend/package*.json ./
RUN npm install

COPY frontend/ ./
RUN npm run build

# Stage 2: Setup the Python Backend
FROM python:3.11-slim
WORKDIR /app

# Install system dependencies required for building python packages (if any)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy backend requirements
COPY requirements.txt .

# Remove pywebview from requirements since it's a headless Docker container
RUN grep -v pywebview requirements.txt > req_docker.txt && \
    pip install --no-cache-dir -r req_docker.txt

# Copy backend and required directories
COPY backend/ backend/
COPY catalogs/ catalogs/
COPY settings_default.yaml .
COPY components.yaml .

# Create symlink for settings to a persistable data volume
RUN mkdir -p /app/data && \
    ln -s /app/data/settings_user.yaml /app/settings_user.yaml

# Copy the built frontend static files from Stage 1
COPY --from=frontend-builder /app/frontend/dist frontend/dist

# Expose the FastAPI default port
EXPOSE 8000

# Start the uvicorn server on all interfaces
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
