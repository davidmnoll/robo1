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
    "appengineflex.googleapis.com",
    "appengine.googleapis.com",
    "sqladmin.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "containerregistry.googleapis.com"
  ]

  cloud_sql_instance_name   = coalesce(var.cloud_sql_instance_name, "${var.project_id}-api-db")
  cloud_sql_connection_name = "${var.project_id}:${var.region}:${local.cloud_sql_instance_name}"
  db_name                   = coalesce(var.db_name, "robotarena")
  db_user                   = coalesce(var.db_user, "robot")
  database_url              = "postgresql+asyncpg://${local.db_user}:${random_password.db_password.result}@/${local.db_name}?host=/cloudsql/${local.cloud_sql_connection_name}"
  cors_allow_origins_json   = jsonencode(var.cors_allow_origins)
  env_seed_overrides = { for item in [
    { key = "SEED_USERS_JSON", value = var.seed_users_json },
    { key = "SEED_LOBBIES_JSON", value = var.seed_lobbies_json },
    { key = "SEED_BOTS_JSON", value = var.seed_bots_json },
  ] : item.key => item.value if length(trimspace(item.value)) > 0 }
  app_engine_env = merge({
    DATABASE_URL                = local.database_url
    SECRET_KEY                  = random_password.api_secret_key.result
    ACCESS_TOKEN_EXPIRE_MINUTES = tostring(var.access_token_expire_minutes)
    ROS_PUSH_KEY                = var.ros_push_key
    GATEWAY_NAME                = var.gateway_name
    CORS_ALLOW_ORIGINS          = local.cors_allow_origins_json
  }, local.env_seed_overrides)
  region_host_suffix = {
    "us-central1"     = "uc.r.appspot.com"
    "us-east1"        = "ue.r.appspot.com"
    "us-east4"        = "ue.r.appspot.com"
    "us-west2"        = "uw.r.appspot.com"
    "us-west3"        = "uw.r.appspot.com"
    "us-west4"        = "uw.r.appspot.com"
    "europe-west1"    = "ew.r.appspot.com"
    "europe-west2"    = "ew.r.appspot.com"
    "europe-west3"    = "ew.r.appspot.com"
    "asia-northeast1" = "an.r.appspot.com"
    "asia-south1"     = "as.r.appspot.com"
  }
  app_engine_domain_suffix = lookup(local.region_host_suffix, var.region, "uc.r.appspot.com")
  app_engine_url_prefix    = var.project_id
  app_engine_url           = "https://${local.app_engine_url_prefix}.${local.app_engine_domain_suffix}"
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
  keepers = {
    project = var.project_id
  }
}

resource "random_password" "api_secret_key" {
  length  = 32
  special = true
}

resource "google_sql_database_instance" "postgres" {
  name                = local.cloud_sql_instance_name
  project             = var.project_id
  database_version    = "POSTGRES_15"
  region              = var.region
  deletion_protection = var.db_deletion_protection

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
  display_name = "Robot gateway App Engine"
  project      = var.project_id
}

resource "google_project_iam_member" "app_cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.api_runner.email}"
}

resource "google_project_iam_member" "app_logs_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.api_runner.email}"
}

resource "google_app_engine_flexible_app_version" "api" {
  project         = var.project_id
  service         = var.app_engine_service_name
  version_id      = var.app_engine_version_id
  runtime         = "custom"
  service_account = google_service_account.api_runner.email

  deployment {
    container {
      image = var.api_image
    }
  }

  resources {
    cpu       = var.app_engine_cpu
    memory_gb = var.app_engine_memory_gb
    disk_gb   = var.app_engine_disk_size_gb
  }

  manual_scaling {
    instances = 1
  }

  liveness_check {
    path = "/api/health"
  }

  readiness_check {
    path = "/api/health"
  }

  env_variables = local.app_engine_env

  beta_settings = {
    "cloud_sql_instances" = local.cloud_sql_connection_name
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    google_sql_database_instance.postgres,
    google_project_iam_member.app_cloudsql_client,
    google_project_iam_member.app_logs_writer
  ]
}
