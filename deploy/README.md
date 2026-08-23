# Deploying the cliptunnel-mcp repeater

The repeater is a small stdlib-only HTTP service that relays CT3 messages
between the Controller and Agent(s) over HTTPS. It is a zero-knowledge relay:
it authenticates peers via bearer tokens but never decrypts content.

## Requirements

- A server with a public IP (VPS, cloud instance, bare metal).
- A domain name pointing to the server (for automatic TLS via Caddy).
- Docker and Docker Compose installed.

## Quick start (Docker + Caddy — recommended)

```bash
# 1. Clone the repo and enter the deploy directory.
git clone https://github.com/jordi-murgo/cliptunnel-mcp.git
cd cliptunnel-mcp/deploy

# 2. Configure your domain in the Caddyfile.
#    Replace `repeater.example.com` with your actual domain.
sed -i 's/repeater.example.com/your-domain.com/' Caddyfile

# 3. Generate bearer tokens for the Controller and each Agent.
python -c "import secrets; print('ctrl:' + secrets.token_urlsafe(32))"
python -c "import secrets; print('agent-a:' + secrets.token_urlsafe(32))"

# 4. Copy env.example to .env and set your tokens.
cp env.example .env
# Edit .env: REPEATER_TOKENS=ctrl:abc123...,agent-a:xyz987...

# 5. Start the repeater + Caddy (automatic HTTPS).
docker compose up -d

# 6. Verify it's running.
curl -s https://your-domain.com/slot -H "Authorization: Bearer ctrl:abc123..."
# {"value": "", "revision": 0}
```

Caddy automatically provisions a Let's Encrypt TLS certificate for your
domain and proxies HTTPS (443) to the repeater (8443) inside the container.

## Without Docker (bare Python)

```bash
# Install the package.
pip install cliptunnel-mcp

# Set tokens and run.
export REPEATER_TOKENS="ctrl:abc123,agent-a:xyz987"
python -m cliptunnel_mcp.repeater

# Run behind a TLS proxy (Caddy, nginx, Cloudflare, etc.).
# The repeater itself listens on plain HTTP at 0.0.0.0:8443.
```

## With Cloudflare Tunnel (no open ports)

If you don't want to expose any ports on your server, use Cloudflare Tunnel
to connect the repeater to a public domain:

```bash
# 1. Install cloudflared.
#    https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/

# 2. Create a tunnel and route your domain to the repeater.
cloudflared tunnel create cliptunnel-repeater
cloudflared tunnel route dns cliptunnel-repeater your-domain.com

# 3. Run the repeater locally.
export REPEATER_TOKENS="ctrl:abc123,agent-a:xyz987"
python -m cliptunnel_mcp.repeater &

# 4. Start the tunnel (proxies your-domain.com → localhost:8443).
cloudflared tunnel run --url http://localhost:8443 cliptunnel-repeater
```

With Cloudflare Tunnel, the repeater stays behind the firewall — no inbound
ports needed. Cloudflare terminates TLS and forwards traffic to the local
repeater.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `REPEATER_TOKENS` | *(required)* | Comma-separated `name:token` pairs. |
| `REPEATER_HOST` | `0.0.0.0` | Bind address. |
| `REPEATER_PORT` | `8443` | Listen port (behind TLS proxy). |

## Endpoints

All endpoints require `Authorization: Bearer <token>`.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/slot` | `POST` | Write a value to the slot. Returns `{"revision": N}`. |
| `/slot` | `GET` | Return the current snapshot `{"value": "...", "revision": N}`. |
| `/slot/events` | `GET` | SSE stream of write events. |

## Security notes

- The repeater is zero-knowledge: it never has the AES key and never sees
  plaintext. Content is encrypted end-to-end between Controller and Agent.
- Bearer tokens are provisioned out-of-band (password manager, signal, etc.).
  They never travel over the CT3 channel.
- If TLS is terminated at the proxy (Caddy, Cloudflare), the link from proxy
  to repeater is plain HTTP within the same host/container network. This is
  safe because the content is already E2E encrypted by AES-256-GCM.
- The repeater state is ephemeral (in-memory). On restart, peers self-heal
  via the agent heartbeat mechanism. No database, no disk.