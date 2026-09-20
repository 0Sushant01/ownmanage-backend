# OwnManage Backend

Backend service foundation for the OwnManage project built with Python, Django, Django REST Framework, SimpleJWT, and Supabase PostgreSQL.

## Prerequisites

- Python 3.12+ (tested with Python 3.14)
- PostgreSQL client libraries (libpq)
- Supabase project with PostgreSQL database access

## 1. Virtual Environment Setup

Create and activate a virtual environment named `.venv`:

```bash
# Create virtual environment
python3 -m venv .venv

# Activate virtual environment (Linux/macOS)
source .venv/bin/activate

# Activate virtual environment (Windows PowerShell)
# .venv\Scripts\Activate.ps1
```

## 2. Dependency Installation

Install pinned foundation dependencies from `requirements.txt`:

```bash
pip install -r requirements.txt
```

## 3. Environment Variables Configuration

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

Edit `.env` and configure your settings:

```env
# Django Configuration
DJANGO_SECRET_KEY=your-secure-secret-key
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1

# Supabase PostgreSQL Database Configuration
DB_NAME=postgres
DB_USER=postgres
DB_PASSWORD=your-supabase-database-password
DB_HOST=your-project-ref.supabase.co
DB_PORT=5432

# CORS Configuration
CORS_ALLOWED_ORIGINS=http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000

# Simple JWT Configuration
JWT_ACCESS_TOKEN_LIFETIME_MINUTES=60
JWT_REFRESH_TOKEN_LIFETIME_DAYS=1
```

## 4. Supabase Database Configuration

In your Supabase project dashboard:
1. Navigate to **Project Settings** > **Database**.
2. Locate the **Connection parameters** (Host, Database name, Port, User, and Password).
3. Populate `DB_HOST`, `DB_NAME`, `DB_PORT`, `DB_USER`, and `DB_PASSWORD` in your `.env` file.

> Note: Local SQLite is explicitly disabled for the application database. Supabase PostgreSQL is the primary database.

## 5. Database Migrations

Once your Supabase PostgreSQL credentials are configured in `.env`, run database migrations:

```bash
python manage.py migrate
```

## 6. Development Server

Run the Django development server:

```bash
python manage.py runserver
```

The API will be available at `http://127.0.0.1:8000/`.