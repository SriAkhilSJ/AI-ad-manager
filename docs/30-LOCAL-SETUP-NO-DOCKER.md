# Local Environment Setup Guide (Native Services — No Docker)

This guide documents running Crusor AI Marketing Director locally using native services on Windows and WSL2/Linux, completely without Docker or containers.

---

## 1. Prerequisites

| Component | Minimum Version | Installation (Windows) | Installation (WSL2 / Ubuntu) |
|---|---|---|---|
| **Python** | 3.11+ | Python 3.11 installer / `winget install Python.Python.3.11` | `sudo apt install python3.11 python3.11-venv python3-pip` |
| **Node.js** | 20+ LTS (or 24) | `winget install OpenJS.NodeJS.LTS` or Scoop | `nvm install 20` or NodeSource |
| **PostgreSQL** | 16 or 17 | EnterpriseDB Windows Installer (port 5432) | `sudo apt install postgresql-16` |
| **pgvector** | 0.8.0+ | MSVC compile via `vcvarsall.bat` + `nmake /F Makefile.win` | `sudo apt install postgresql-16-pgvector` |
| **Redis** | 7.x / 8.x | `scoop install redis` or Memurai | `sudo apt install redis-server` |
| **Git** | 2.40+ | `winget install Git.Git` or Scoop | `sudo apt install git` |

---

## 2. PostgreSQL 16/17 with pgvector Setup

### Windows Native

1. **Verify PostgreSQL Service**:
   Ensure PostgreSQL is running as a Windows service on port 5432:
   ```powershell
   Get-Service -Name postgresql*
   ```
2. **Build and Install pgvector**:
   Clone and build `pgvector` with MSVC:
   ```cmd
   git clone --branch v0.8.0 https://github.com/pgvector/pgvector.git C:\pgvector
   cd /d C:\pgvector
   call "C:\Program Files\Microsoft Visual Studio\18\Community\VC\Auxiliary\Build\vcvarsall.bat" x64
   set "PGROOT=C:\Program Files\PostgreSQL\17"
   nmake /F Makefile.win
   nmake /F Makefile.win install
   ```
3. **Enable Extension**:
   ```sql
   CREATE DATABASE crusor_dev;
   \c crusor_dev;
   CREATE EXTENSION IF NOT EXISTS vector;
   SELECT extversion FROM pg_extension WHERE extname = 'vector';
   ```

### WSL2 / Linux Native

```bash
sudo apt update
sudo apt install -y postgresql-16 postgresql-16-pgvector
sudo systemctl enable --now postgresql
sudo -u postgres psql -c "CREATE USER crusor WITH PASSWORD 'crusor_password';"
sudo -u postgres psql -c "CREATE DATABASE crusor_dev OWNER crusor;"
sudo -u postgres psql -d crusor_dev -c "CREATE EXTENSION IF NOT EXISTS vector;"
```

---

## 3. Redis Setup

### Windows Native

Install and launch Redis via Scoop:
```powershell
scoop install redis
Start-Process "redis-server" -WindowStyle Hidden
redis-cli ping  # Returns PONG
```

### WSL2 / Linux Native

```bash
sudo apt install -y redis-server
sudo systemctl enable --now redis-server
redis-cli ping  # Returns PONG
```

---

## 4. Backend Setup

1. **Virtual Environment**:
   ```powershell
   python -m venv .venv --clear
   .\.venv\Scripts\activate
   ```

2. **Install Dependencies**:
   ```powershell
   python -m pip install --upgrade pip
   pip install -r backend/requirements.txt
   ```

3. **Configure Environment (`.env`)**:
   Copy `.env.example` to `.env` in the repository root:
   ```env
   APP_NAME="Crusor AI Marketing Director"
   ENVIRONMENT=development
   DEBUG=true
   API_V1_PREFIX=/api/v1
   CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173

   SECRET_KEY=change-me-dev-only-secret-key-at-least-32-chars-0123456789
   BOOTSTRAP_FOUNDER_EMAIL=founder@crusor.ai
   BOOTSTRAP_FOUNDER_PASSWORD=change-me-dev-password

   # SQLite (default) or PostgreSQL:
   DATABASE_URL=sqlite+aiosqlite:///./.data/crusor.db
   # For PostgreSQL:
   # DATABASE_URL=postgresql+asyncpg://postgres:akhil@127.0.0.1:5432/crusor_dev

   # Redis & Celery
   REDIS_URL=redis://127.0.0.1:6379/0
   CELERY_BROKER_URL=redis://127.0.0.1:6379/0
   CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/1
   TASK_DISPATCHER=celery

   STORAGE_BACKEND=local
   FLOW_LIVE_GENERATION_ENABLED=false
   CREATIVE_ALLOW_PAID_GENERATION=false
   TOKEN_VAULT_BACKEND=none
   ```

4. **Database Migrations**:
   ```powershell
   cd backend
   mkdir -p .data
   ..\.venv\Scripts\alembic upgrade head
   ```

5. **Seed Brand Data**:
   ```powershell
   ..\.venv\Scripts\python -m app.seed.brand_crusor
   ```

---

## 5. Frontend Setup

1. **Install Dependencies**:
   ```powershell
   cd frontend
   npm install
   ```

2. **Typecheck & Production Build**:
   ```powershell
   npm run typecheck
   npm run build
   ```

---

## 6. Celery Worker Setup

Start the Celery worker bound to the application tasks:
```powershell
# Windows: use --pool=solo
cd backend
..\.venv\Scripts\celery -A app.tasks.celery_app:celery_app worker --pool=solo --loglevel=info
```

Verify broker roundtrip:
```powershell
cd backend
..\.venv\Scripts\python ..\scripts\celery_roundtrip.py
```

---

## 7. Starting the Complete Application

Open three separate terminals:

### Terminal 1: Backend API
```powershell
cd backend
..\.venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
# Verify: curl http://localhost:8000/docs
```

### Terminal 2: Celery Worker
```powershell
cd backend
..\.venv\Scripts\celery -A app.tasks.celery_app:celery_app worker --pool=solo --loglevel=info
```

### Terminal 3: Frontend Dev Server
```powershell
cd frontend
npm run dev
# Access: http://localhost:5173
```

---

## 8. Test Execution Commands

```powershell
# 1. Full Backend Test Suite
cd backend
..\.venv\Scripts\pytest tests -q -p no:warnings

# 2. Database Migration Tests
..\.venv\Scripts\alembic downgrade -1
..\.venv\Scripts\alembic upgrade head
..\.venv\Scripts\alembic current

# 3. Celery Broker Roundtrip
..\.venv\Scripts\python ..\scripts\celery_roundtrip.py

# 4. Frontend Typecheck and Build
cd ..\frontend
npm run typecheck
npm run build

# 5. Security Guard (No Hardcoded Secrets)
git grep -nE '(sk-[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|EAA[A-Za-z0-9]{40,})' -- . ':!*.md' ':!docs'
```

---

## 9. Troubleshooting

| Issue | Cause | Resolution |
|---|---|---|
| `Exit code -1073741502 (0xC0000142)` | Windows Session 0 / DLL initialization failure | Restart the host or Antigravity IDE process to clear console handle locks. |
| Celery crashes with `ValueError` on Windows | `prefork` pool is unsupported on Windows | Always start Celery with `--pool=solo`. |
| Rollup build error in `react-dom` | Torn write during npm extraction | Clean npm cache and reinstall: `npm install react-dom@18.3.1 --prefer-online`. |
| Missing `.data` directory for SQLite | Parent directory does not exist | Run `mkdir -p .data` inside `backend/` before running Alembic. |
| Redis connection refused | Redis server is not running | Start Redis: `Start-Process redis-server -WindowStyle Hidden`. |
