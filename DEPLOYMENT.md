# Alfred on Supabase

This repo can now run in two storage modes:

- `sqlite`: local default for laptop/dev usage.
- `postgres`: enabled automatically when `ALFRED_DATABASE_URL` or `DATABASE_URL` is set.

For Supabase, Alfred uses the project's Postgres database directly. It does not require a Supabase client SDK in the app layer.

## 1. Create the Supabase database

Create a Supabase project, then copy a Postgres connection string from the project dashboard.

- Use the direct connection string when your server supports IPv6 and you want a normal long-lived Postgres connection.
- Use the Supavisor session pooler string when your app server is IPv4-only or you want pooled connections.

## 2. Configure Alfred

Set these environment variables on the machine or container running Alfred:

```env
ALFRED_DATABASE_URL=postgresql://...
ALFRED_JWT_SECRET=replace-with-a-long-random-secret
ALFRED_INTEGRATIONS_SECRET=replace-with-a-second-long-random-secret
ALFRED_COOKIE_SECURE=true
ALFRED_TRUSTED_ORIGINS=https://alfred.your-domain.example
ALFRED_AUTOSTART_WHATSAPP_BRIDGE=false
```

Notes:

- `ALFRED_JWT_SECRET` must be stable across restarts or all web sessions reset.
- `ALFRED_INTEGRATIONS_SECRET` must be stable across restarts or saved Gmail credentials cannot be decrypted.
- `ALFRED_AUTOSTART_WHATSAPP_BRIDGE=false` is the safe default for a dedicated Alfred API server. Run the WhatsApp bridge separately if you want that feature in production.

## 3. Migrate existing local data

If you already have Alfred data in `users/alfred.db`, Alfred will import it into Postgres automatically on first startup when:

- `ALFRED_DATABASE_URL` is set, and
- the target Postgres tables are empty.

If your old SQLite file is somewhere else, point Alfred at it before first startup:

```env
ALFRED_SQLITE_SOURCE_PATH=/absolute/path/to/alfred.db
```

## 4. Run Alfred as its own server

### Docker

Build the image:

```bash
docker build -t alfred .
```

Run it:

```bash
docker run --env-file .env -p 8000:8000 alfred
```

The container starts:

```bash
uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
```

### Bare VM

On a Linux VM with Python installed:

```bash
pip install -r requirements.txt
ALFRED_DATABASE_URL=postgresql://... uvicorn main:app --host 0.0.0.0 --port 8000
```

Put a reverse proxy such as Caddy or Nginx in front of it for TLS and a stable public domain.

## 5. Production checklist

- Set `ALFRED_COOKIE_SECURE=true`.
- Set `ALFRED_TRUSTED_ORIGINS` to your real HTTPS origin.
- Keep `ALFRED_ENABLE_TERMINAL_COMMANDS=false`.
- Disable WhatsApp bridge autostart unless you are intentionally operating it on the server.
- Let Supabase handle managed Postgres backups; Alfred's local file backup path only applies to SQLite mode.
- Run `python prod_fixes.py` once after first deployment to apply DB indexes.
- Use the Docker healthcheck: `curl -f http://localhost:8000/health/ready`
- For multi-instance deployments, replace the in-memory rate limiter with Redis by setting `REDIS_URL`.
