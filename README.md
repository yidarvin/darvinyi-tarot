### tarot
A multi-user tarot reading web app powered by Claude. Each user stores their own Anthropic API key; the app uses it to stream card interpretations and spread narratives personalised to their onboarding profile.

### Prerequisites
- **Python**: 3.11+
- **Docker**: 24+ and **Docker Compose** (v2) — for local containerised dev
- **PostgreSQL** — provided automatically when deploying to Railway

---

### Deploy to Railway

1. **Fork** this repository to your GitHub account.

2. **Create a new Railway project** at [railway.app](https://railway.app) and connect your forked repo.

3. **Add the PostgreSQL plugin** — in the Railway dashboard click *New* → *Database* → *PostgreSQL*. Railway automatically injects `DATABASE_URL` into your app's environment.

4. **Set the three required environment variables** in Railway → *Variables*:

   | Variable | How to generate |
   |----------|----------------|
   | `SECRET_KEY` | `python -c "import secrets; print(secrets.token_hex(32))"` |
   | `FERNET_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
   | `DATABASE_URL` | Set automatically by the Postgres plugin — no action needed |

5. **Deploy** — Railway builds from the `Dockerfile` and starts the app. Startup runs `flask db upgrade` before launching gunicorn, so schema migrations are applied automatically.

6. Visit the Railway-provided URL, register an account, and enter your Anthropic API key during onboarding.

---

### Run locally with a virtual environment

1. Copy `.env.example` to `.env` and fill in the values:
   ```
   cp .env.example .env
   ```

2. Provision a local Postgres database and set `DATABASE_URL` in `.env`, e.g.:
   ```
   DATABASE_URL=postgresql://postgres:password@localhost:5432/tarot
   ```

3. Generate `SECRET_KEY` and `FERNET_KEY` (commands in `.env.example`) and add them to `.env`.

4. Create a venv and install deps:
   ```
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

5. Run tests:
   ```
   python -m unittest discover -s tests -p "test_*.py" -v
   ```

6. Apply migrations:
   ```
   FLASK_APP=app:create_app flask db upgrade
   ```

7. Run the web app:
   ```
   python app.py
   ```
   Visit `http://localhost:5000`, register, and enter your Anthropic API key during onboarding.

### Run the CLI (spread.py)

`spread.py` still works as a standalone CLI for quick card draws without a database:
```
export ANTHROPIC_API_KEY=sk-ant-...   # optional — skips interpretation if unset
export PATH_TO_SAVE=/tmp/tarot_saves  # optional — where markdown exports are written
python spread.py 3card --seed 42 --no-interpret
python spread.py celticcross --reversal-prob 0.4
```

### Run with Docker Compose (local)

```
cp .env.example .env   # fill in SECRET_KEY, FERNET_KEY, DATABASE_URL
docker compose build
docker compose up
```
Then open `http://localhost:5000`.

### Notes
- Card images are bundled under `cards/standard` and served at `/cards/<filename>`.
- The app uses gunicorn with `gthread` workers and `--timeout 120` to support long-lived SSE streams.
- Database schema is managed via Flask-Migrate in `migrations/`; run `flask db upgrade` after pulling schema changes.
