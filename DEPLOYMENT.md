# Deployment (orb-trader)

This repo includes:
- `Dockerfile` for building a runnable container
- `docker-compose.yml` for local testing
- `config/deploy.yml` for **Kamal 2** deployments

## Prerequisites

### Local
- Docker Desktop (or Docker Engine)
- `docker compose` plugin

### Server
- A Linux server with Docker installed (Ubuntu/Debian/etc)
- SSH access from your laptop
- (Optional) a container registry (Docker Hub, GHCR, ECR, etc)

### Kamal
- Install Kamal 2 locally:
  ```bash
  gem install kamal
  kamal version
  ```

## Secrets / credentials

This app **does not bake secrets into the image**.

Runtime secrets are expected at:
- `config/secrets.yaml` (mounted into the container)
- `config/schwab_token.json` (mounted into the container; generated/updated after auth)

The repo already git-ignores:
- `config/secrets.yaml`
- `config/schwab_token.json`

### Local secrets
Create/edit:
- `config/secrets.yaml` (copy from `config/secrets.yaml.example`)

### Server secrets
On the server, create:
- `/opt/orb-trader/config/secrets.yaml`
- `/opt/orb-trader/config/schwab_token.json` (can start as `{}`)
- `/opt/orb-trader/logs/` directory

Example:
```bash
sudo mkdir -p /opt/orb-trader/config /opt/orb-trader/logs
sudo nano /opt/orb-trader/config/secrets.yaml
sudo bash -c 'echo {} > /opt/orb-trader/config/schwab_token.json'
sudo chown -R $USER:$USER /opt/orb-trader
```

## Local Docker testing

Build + run:
```bash
docker compose up --build
```

Run detached:
```bash
docker compose up -d --build
```

View logs:
```bash
docker compose logs -f --tail=200
```

Stop:
```bash
docker compose down
```

Healthcheck:
```bash
curl -fsS http://localhost:8000/health
```

## Kamal deployment (basic)

1) Edit `config/deploy.yml`:
- Set `image:` to your registry/repo (ex: `ghcr.io/you/orb-trader`)
- Set `registry.server:`
- Replace the placeholder server IP under `servers.trader.hosts`
- Confirm the `volumes:` paths match where you created secrets/logs on the server

2) Set registry creds locally (or use `kamal secrets`):
```bash
export KAMAL_REGISTRY_USERNAME=... 
export KAMAL_REGISTRY_PASSWORD=...
```

3) Deploy:
```bash
kamal deploy
```

4) Tail logs:
```bash
kamal app logs -f
```

### Updating / redeploying

After code changes:
```bash
kamal deploy
```

To restart containers without a rebuild/push:
```bash
kamal app restart
```

To run a one-off command in the container:
```bash
kamal app exec "python -m src.main"
```

## Logs

### Local
- Log file in the container: `/app/logs/orb-trader.log`
- Mounted to host: `./logs/orb-trader.log`

### Server
- Mounted on host: `/opt/orb-trader/logs/orb-trader.log`
- Or via Kamal:
  ```bash
  kamal app logs -f
  ```

## Cron setup (9:25 AM America/New_York)

`orb-trader` can run continuously (it sleeps/polls and resets state daily), but if you
prefer to only run it during market hours you can use cron to start it.

### Option A (recommended): keep it running
Let Docker/Kamal restart policies keep the process alive. This is the simplest
operationally.

### Option B: start it via cron at 09:25 ET
On the **server**, add a crontab entry that runs on weekdays.

Example (runs a restart at 09:25 ET so the process is “fresh” going into the open):
```cron
# Weekdays at 09:25 America/New_York
25 9 * * 1-5 cd /path/to/your/checked-out/repo && kamal app restart
```

Notes:
- Cron uses the server timezone. Ensure the server is set to `America/New_York`, or
  adjust the schedule accordingly.
- If you want cron to start/stop containers instead, you can use `kamal app start` /
  `kamal app stop` (depending on your Kamal version) or manage via systemd.

---

## Healthcheck endpoint

The container exposes a minimal HTTP endpoint used by Docker/Kamal healthchecks:
- `GET /health` → `200 ok`

It runs inside the same process (background thread) and is controlled by:
- `HEALTH_PORT` env var (default `8000`)
