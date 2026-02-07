# Local Reverse Proxy and Quick Tunnel

This stack uses a single ingress service (`reverse-proxy`) for local access and an optional Cloudflare Quick Tunnel for public callback testing.

## Services
- `api` (FastAPI): internal app service on `4000`
- `admin-ui` (Next.js): internal app service on `4100`
- `reverse-proxy` (Caddy): ingress on host `4080`
- `cloudflared`: optional public tunnel targeting `reverse-proxy`

## Route matrix
- `http://localhost:4080/api/*` -> `api:4000`
- `http://localhost:4080/health` -> `api:4000`
- `http://localhost:4080/jira/*` -> `api:4000`
- `http://localhost:4080/runs*` -> `api:4000`
- everything else (`/login`, `/tenants`, `/privacy`, etc.) -> `admin-ui:4100`

## Start
```bash
docker compose up --build
```

## Verify
```bash
curl -i http://localhost:4080/health
curl -i http://localhost:4080/login
```

## Quick Tunnel URL
`cloudflared` emits the public URL in container logs.

Watch logs:
```bash
docker compose logs -f cloudflared
```

Extract URL only:
```bash
docker compose logs cloudflared | rg -o "https://[-a-z0-9]+\\.trycloudflare\\.com" | tail -n 1
```

Use the returned URL for local callback testing only.
