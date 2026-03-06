Goal: 
---

Experiment with creating a dashboard to remotely control simulated robots: 



![Screenshot](./docs/screenshot1.png)

## Authentication & lobbies

- Users can register/login/logout via the dashboard (or directly against the FastAPI endpoints under `/api/auth/*`). Credentials are stored in PostgreSQL with bcrypt hashes and JWT tokens secure subsequent calls.
- After signing in, the dashboard lists existing lobbies. Creating a lobby stores ROS bridge connection info and returns an access key you can share with operators/bots.
- Lobby APIs:
  - `POST /api/auth/register` and `POST /api/auth/login` → returns `{ access_token, user }`
  - `GET /api/lobbies` → lists every lobby (the access key only shows for the owner)
  - `POST /api/lobbies` → create a lobby with `name`, `ros_host`, `ros_port`, optional `description`
- Bot APIs:
  - `GET /api/bots` → list every registered bot along with its lobby/owner metadata
  - `POST /api/bots` → register a bot (name + ROS namespace) under a lobby you own
- Internal ROS handshake:
  - `POST /api/internal/lobbies/{lobby_name}/online` with the lobby `access_key` kicks off the gateway’s ROS bridge connection monitor so Cloud Run can wait for rosbridge to come online after deploy.
- The dashboard now includes a bot management card plus a dropdown in the teleop panel so you can pick a registered bot namespace (or fall back to manual entry).
- The gateway can seed users, lobbies, and bots from JSON provided via `SEED_USERS_JSON` / `SEED_LOBBIES_JSON` / `SEED_BOTS_JSON`. The default compose file seeds `dmn322` / `TEST123!`, a `ros-core` lobby whose `access_key` reuses the shared `ROS_PUSH_KEY` env var (fed into both the ROS camera forwarder and `ROS_PUSH_KEY` on the API), and two bots (`bot_alpha`, `bot_beta`) that match the simulated robots. Example:

```bash
export ROS_PUSH_KEY=super-secret
export SEED_USERS_JSON='[{"email":"dmn322","password":"TEST123!"}]'
export SEED_LOBBIES_JSON='[{"name":"ros-core","ros_host":"ros-core","ros_port":9090,"description":"Default ROS core lobby","access_key":"'"$ROS_PUSH_KEY"'","owner_email":"dmn322"}]'
export SEED_BOTS_JSON='[{"name":"Arena Bot Alpha","ros_namespace":"bot_alpha","lobby_name":"ros-core","owner_email":"dmn322"},{"name":"Arena Bot Beta","ros_namespace":"bot_beta","lobby_name":"ros-core","owner_email":"dmn322"}]'
```
- The `ros-core` service runs `rosbridge_server` plus a `camera_forwarder` ROS 2 node. The node subscribes to `CAMERA_NAMESPACES` (comma-separated list of robot namespaces) and pushes base64 frames to `API_PUSH_URL` using `ROS_PUSH_KEY` for authentication.
- The `sim` service is wired to `ros-core` via `ROS_BRIDGE_HOST=ros-core` / `ROS_BRIDGE_PORT=9090` so its controllers publish frames over rosbridge without any manual tweaks.

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
