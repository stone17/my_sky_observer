cd frontend
call npm ci --ignore-scripts
call npm run build
cd ..
uvicorn backend.main:app --reload