terraform {
  required_version = ">= 1.6.0"

  backend "gcs" {
    bucket = "robo1-terraform-state"
    prefix = "env/prod"
  }

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.31"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  required_services = [
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com"
  ]

  cloud_sql_instance_name   = coalesce(var.cloud_sql_instance_name, "${var.project_id}-api-db")
  cloud_run_service_name    = coalesce(var.cloud_run_service_name, "robot-gateway")
  cloud_sql_connection_name = "${var.project_id}:${var.region}:${local.cloud_sql_instance_name}"
  db_name                   = coalesce(var.db_name, "robotarena")
  db_user                   = coalesce(var.db_user, "robot")
  database_url              = "postgresql+asyncpg://${local.db_user}:${random_password.db_password.result}@/${local.db_name}?host=/cloudsql/${local.cloud_sql_connection_name}"
  cors_allow_origins_json   = jsonencode(var.cors_allow_origins)
}

resource "google_project_service" "enabled" {
  for_each = toset(local.required_services)
  project  = var.project_id
  service  = each.value

  disable_on_destroy = false
}

resource "random_password" "db_password" {
  length  = 24
  special = true
}

resource "random_password" "api_secret_key" {
  length  = 32
  special = true
}

resource "google_sql_database_instance" "postgres" {
  name             = local.cloud_sql_instance_name
  project          = var.project_id
  database_version = "POSTGRES_15"
  region           = var.region

  settings {
    tier = var.db_instance_tier
    ip_configuration {
      ipv4_enabled = true
    }
    disk_autoresize = true
    disk_size       = var.db_disk_size_gb
  }

  depends_on = [google_project_service.enabled]
}

resource "google_sql_database" "robotarena" {
  name     = local.db_name
  instance = google_sql_database_instance.postgres.name
  project  = var.project_id
}

resource "google_sql_user" "app" {
  name     = local.db_user
  instance = google_sql_database_instance.postgres.name
  project  = var.project_id
  password = random_password.db_password.result
}

resource "google_service_account" "api_runner" {
  account_id   = "robot-gateway"
  display_name = "Robot gateway Cloud Run"
  project      = var.project_id
}

resource "google_project_iam_member" "run_cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.api_runner.email}"
}

resource "google_cloud_run_service" "api" {
  name     = local.cloud_run_service_name
  location = var.region
  project  = var.project_id

  metadata {
    annotations = {
      "run.googleapis.com/ingress" = "all"
    }
  }

  template {
    metadata {
      annotations = {
        "run.googleapis.com/cloudsql-instances" = local.cloud_sql_connection_name
      }
    }

    spec {
      service_account_name = google_service_account.api_runner.email
      containers {
        image = var.api_image

        env {
          name  = "DATABASE_URL"
          value = local.database_url
        }

        env {
          name  = "SECRET_KEY"
          value = random_password.api_secret_key.result
        }

        env {
          name  = "ACCESS_TOKEN_EXPIRE_MINUTES"
          value = tostring(var.access_token_expire_minutes)
        }

        env {
          name  = "ROS_BRIDGE_HOST"
          value = var.ros_bridge_host
        }

        env {
          name  = "ROS_BRIDGE_PORT"
          value = tostring(var.ros_bridge_port)
        }

        env {
          name  = "ROS_PUSH_KEY"
          value = var.ros_push_key
        }

        env {
          name  = "GATEWAY_NAME"
          value = var.gateway_name
        }

        env {
          name  = "CORS_ALLOW_ORIGINS"
          value = local.cors_allow_origins_json
        }

        dynamic "env" {
          for_each = { for k, v in {
            SEED_USERS_JSON   = var.seed_users_json
            SEED_LOBBIES_JSON = var.seed_lobbies_json
            SEED_BOTS_JSON    = var.seed_bots_json
          } : k => v if length(trimspace(v)) > 0 }

          content {
            name  = env.key
            value = env.value
          }
        }

        resources {
          limits = {
            cpu    = var.cloud_run_cpu
            memory = var.cloud_run_memory
          }
        }
      }
    }
  }

  traffic {
    percent         = 100
    latest_revision = true
  }

  depends_on = [
    google_project_service.enabled,
    google_sql_database_instance.postgres,
    google_project_iam_member.run_cloudsql_client
  ]
}

resource "google_cloud_run_service_iam_member" "public_invoker" {
  project  = google_cloud_run_service.api.project
  location = google_cloud_run_service.api.location
  service  = google_cloud_run_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}
