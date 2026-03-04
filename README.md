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
- The dashboard now includes a bot management card plus a dropdown in the teleop panel so you can pick a registered bot namespace (or fall back to manual entry).
- The gateway can seed users and lobbies from JSON provided via `SEED_USERS_JSON`/`SEED_LOBBIES_JSON`. The default compose file seeds `dmn322` / `TEST123!` and a `ros-core` lobby whose `access_key` reuses the shared `ROS_LOBBY_KEY` env var (fed into both `ROS_PROXY_KEYS` on ros-core and `ROS_PROXY_KEY` on the API). Example:

```bash
export ROS_LOBBY_KEY=super-secret
export SEED_USERS_JSON='[{"email":"dmn322","password":"TEST123!"}]'
export SEED_LOBBIES_JSON='[{"name":"ros-core","ros_host":"ros-core","ros_port":9090,"description":"Default ROS core lobby","access_key":"'"$ROS_LOBBY_KEY"'","owner_email":"dmn322"}]'
```

## Data flow & stack

```mermaid
flowchart LR
    subgraph SimContainer
        W[Webots E-puck controllers]
    end

    subgraph ROSCore
        R[rosbridge_server + proxy]
    end

    subgraph Gateway
        A[FastAPI + roslibpy + aiortc]
    end

    subgraph Dashboard
        B[Browser WebRTC client]
    end

    W -- sensor_msgs/Image --> R
    R -- ROS bridge WebSocket --> A
    A -- WebRTC video track --> B
    B -- REST/WebSocket commands --> A
    A -- cmd_vel via rosbridge --> R
```

## Database

- The stack now ships with a PostgreSQL container (`db` service) provisioned via `docker-compose`. The FastAPI gateway uses `DATABASE_URL=postgresql+asyncpg://robot:robot@db:5432/robotarena`.
- When running outside Docker/tmux, point `DATABASE_URL` at your own Postgres instance (e.g. `export DATABASE_URL=postgresql+asyncpg://robot:robot@localhost:5432/robotarena`) and set `SECRET_KEY`.
- Tables are auto-created on startup; no separate migration step is required for local dev.
