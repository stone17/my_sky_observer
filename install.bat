cd frontend
call npm install
call npm run build
cd ..
uvicorn backend.main:app --reload