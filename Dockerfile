FROM python:3.11-slim

WORKDIR /app

# Unbuffered output and no .pyc files
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# psycopg2-binary is self-contained (bundles libpq), so no apt packages needed.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

# Run migrations at boot, then start Gunicorn.
# Use shell form so $PORT (injected by Railway at runtime) is expanded.
# --worker-class gthread lets a single process serve several concurrent SSE
# streams without blocking; --timeout 120 keeps long-lived LLM streams alive.
CMD FLASK_APP=app:create_app flask db upgrade && \
    gunicorn "app:create_app()" \
    --bind "0.0.0.0:${PORT:-5000}" \
    --workers 1 \
    --worker-class gthread \
    --threads 4 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
