Goal: 
---

Experiment with creating a dashboard to remotely control simulated robots: 



![Screenshot](./docs/screenshot1.png)

## Authentication & lobbies

- Users can register/login/logout via the dashboard (or directly against the FastAPI endpoints under `/api/auth/*`). Credentials are stored in PostgreSQL with bcrypt hashes and JWT tokens secure subsequent calls.
- After signing in, the dashboard lists existing lobbies. Creating a lobby stores metadata (name, description, public/private flag) and returns an access key you can share with operators/bots.
- Lobby APIs:
  - `POST /api/auth/register` and `POST /api/auth/login` → returns `{ access_token, user }`
  - `GET /api/lobbies` → lists every lobby you can access (owners see private lobbies plus their keys)
  - `POST /api/lobbies` → create a lobby with `name`, optional `description`, and `is_public`
  - `GET /api/lobbies/{id}` → fetch lobby details plus registered robots (only available to the owner or when the lobby is public)
  - `PATCH /api/lobbies/{id}` / `DELETE /api/lobbies/{id}` → edit or soft-delete lobbies you own
- Bot APIs:
  - `GET /api/bots` → list active bots (filtered to non-deleted entries)
  - `POST /api/bots` → register a bot (name + ROS namespace) under a lobby you own
  - `PATCH /api/bots/{id}` / `DELETE /api/bots/{id}` → edit or soft-delete bot metadata
- Internal ROS handshake:
  - `POST /api/internal/lobbies/{lobby_name}/online` with the lobby `access_key` simply validates ownership so bots can pair using the shared key.
  - Bridges push camera frames (gzip-compressed + base64) via `POST /api/internal/frames/{robot_namespace}` and maintain a persistent WebSocket connection to `/api/internal/ws/lobbies?api_key=...`. The API pushes queued velocity commands down that socket (tagged by robot namespace), and the bridge responds with `complete` messages after publishing to rosbridge. Heartbeats are also sent over the same WebSocket channel, so no extra polling endpoints are required.
- The dashboard ships with lobby management and teleoperation views so owners can copy lobby keys, tweak privacy, and curate bots from the browser.
- The gateway can seed users, lobbies, and bots from JSON provided via `SEED_USERS_JSON` / `SEED_LOBBIES_JSON` / `SEED_BOTS_JSON`. The default compose file seeds `dmn322` / `TEST123!`, a `ros-core` lobby whose `access_key` reuses the shared `ROS_PUSH_KEY` env var (fed into both the ROS camera forwarder and `ROS_PUSH_KEY` on the API), and two bots (`bot_alpha`, `bot_beta`) that match the simulated robots. Example:

```bash
export ROS_PUSH_KEY=super-secret
export SEED_USERS_JSON='[{"email":"dmn322","password":"TEST123!"}]'
export SEED_LOBBIES_JSON='[{"name":"ros-core","description":"Default ROS core lobby","access_key":"'"$ROS_PUSH_KEY"'","owner_email":"dmn322","is_public":true}]'
export SEED_BOTS_JSON='[{"name":"Arena Bot Alpha","ros_namespace":"bot_alpha","lobby_name":"ros-core","owner_email":"dmn322"},{"name":"Arena Bot Beta","ros_namespace":"bot_beta","lobby_name":"ros-core","owner_email":"dmn322"}]'
```
- The `ros-core` service runs `rosbridge_server` plus a bridge node that pushes camera frames and opens an authenticated WebSocket to `/api/internal/ws/lobbies` for command delivery. Set `API_BASE_URL` (default `http://robot-gateway:8080/api`) and `ROS_PUSH_KEY` so the node can authenticate when hitting `/api/internal/*` endpoints.
- The `sim` service still connects to rosbridge via `ROS_BRIDGE_HOST=ros-core` / `ROS_BRIDGE_PORT=9090`, but the API no longer needs public rosbridge credentials because robots initiate every connection.
- Need to run the Webots arena locally with the GUI (e.g., to debug camera angles)? Use `make -f sim/Makefile.remote remote-sim API_URL=https://<gateway>/api LOBBY_KEY=<key>` after installing Webots + ROS 2 on your machine. The command starts the ROS bridge + camera forwarder locally and launches the `battle_arena.wbt` world so your robots attach to the supplied lobby.

## Data flow & stack

```mermaid
flowchart LR
    subgraph SimContainer
        W[Webots E-puck controllers]
    end

    subgraph ROSCore
        R[rosbridge_server]
        F[CameraForwarder node]
    end

    subgraph Gateway
        A[FastAPI + roslibpy + aiortc]
    end

    subgraph Dashboard
        B[Browser WebRTC client]
    end

    W -- sensor_msgs/Image --> R
    R -- ROS bridge WebSocket --> F
    F -- HTTP frame push --> A
    A -- WebRTC video track --> B
    B -- REST/WebSocket commands --> A
    A -- cmd_vel via rosbridge --> R
```

## Database

- The stack now ships with a PostgreSQL container (`db` service) provisioned via `docker-compose`. The FastAPI gateway uses `DATABASE_URL=postgresql+asyncpg://robot:robot@db:5432/robotarena`.
- When running outside Docker/tmux, point `DATABASE_URL` at your own Postgres instance (e.g. `export DATABASE_URL=postgresql+asyncpg://robot:robot@localhost:5432/robotarena`) and set `SECRET_KEY`.
- Tables are auto-created on startup; no separate migration step is required for local dev.

## Cloud deployment

- Infrastructure-as-code for the hosted API/database lives under `terraform/`. It provisions Cloud SQL + Cloud Run inside the `robo1-489405` project.
- Terraform state is stored in the `robo1-terraform-state` GCS bucket; the GitHub Actions workflow (`.github/workflows/terraform.yml`) ensures the bucket exists before running `terraform init`.
- The workflow authenticates with the `GCP_TERRAFORM_TOKEN` secret (a JSON key for the Terraform service account), ensures the API container image (`gcr.io/robo1-489405/robot-gateway:${GITHUB_SHA}`) exists by building/pushing it when necessary, and automatically runs `terraform plan`/`apply` on pushes to `main`.
- Customize runtime values (API image, CORS origins, ROS bridge host, etc.) via Terraform variables or environment overrides detailed in `terraform/README.md`.
- Need to inspect the hosted Postgres instance? Run `make CLOUD_SQL_INSTANCE="project:region:instance" CLOUD_SQL_USER="arena_app" cloud-sql-shell` (optionally override `CLOUD_SQL_DB`). If Terraform state is available locally, those values are inferred automatically before running `gcloud sql connect`.
- To run the ROS bridge + Webots sim containers against the hosted Cloud Run API (instead of the local stack), set `CLOUD_RUN_LOBBY_KEY=<lobby access key>` and run `make cloud-sim`. If a Terraform state directory is present it automatically pulls the Cloud Run URL; otherwise you can override with `CLOUD_RUN_API_URL=https://robot-gateway-.../api`. This uses `docker-compose.cloud.yml` to point `ros-core` and `sim` at the remote API so you can generate telemetry for an existing lobby.
