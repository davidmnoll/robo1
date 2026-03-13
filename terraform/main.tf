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
    "sqladmin.googleapis.com",
    "compute.googleapis.com",
    "iam.googleapis.com",
    "artifactregistry.googleapis.com",
    "containerregistry.googleapis.com",
    "iap.googleapis.com"
  ]

  cloud_sql_instance_name   = coalesce(var.cloud_sql_instance_name, "${var.project_id}-api-db")
  cloud_sql_connection_name = "${var.project_id}:${var.region}:${local.cloud_sql_instance_name}"
  db_name                   = coalesce(var.db_name, "robotarena")
  db_user                   = coalesce(var.db_user, "robot")
  cors_allow_origins_json   = jsonencode(var.cors_allow_origins)
  env_seed_overrides = { for item in [
    { key = "SEED_USERS_JSON", value = var.seed_users_json },
    { key = "SEED_LOBBIES_JSON", value = var.seed_lobbies_json },
    { key = "SEED_BOTS_JSON", value = var.seed_bots_json },
  ] : item.key => item.value if length(trimspace(item.value)) > 0 }
  api_env = merge({
    DATABASE_URL                = "postgresql+asyncpg://${local.db_user}:${random_password.db_password.result}@${google_sql_database_instance.postgres.ip_address[0].ip_address}/${local.db_name}"
    SECRET_KEY                  = random_password.api_secret_key.result
    ACCESS_TOKEN_EXPIRE_MINUTES = tostring(var.access_token_expire_minutes)
    ROS_PUSH_KEY                = var.ros_push_key
    GATEWAY_NAME                = var.gateway_name
    CORS_ALLOW_ORIGINS          = local.cors_allow_origins_json
    STUN_SERVER                 = "${google_compute_address.api_ip.address}:3478"
  }, local.env_seed_overrides)
  api_env_content = join("\n", [for k, v in local.api_env : "${k}=${v}"])
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
      authorized_networks {
        name  = "api-vm"
        value = google_compute_address.api_ip.address
      }
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

resource "google_compute_address" "api_ip" {
  name    = "${var.api_vm_name}-ip"
  region  = var.region
  project = var.project_id
}

# Bump the value below to force VM recreation
resource "null_resource" "vm_recreate_trigger" {
  triggers = {
    version = "2"
  }
}


resource "google_service_account" "api_vm" {
  account_id   = "robot-gateway-vm"
  display_name = "Robot gateway VM"
  project      = var.project_id
}

resource "google_project_iam_member" "api_vm_logs_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.api_vm.email}"
}

resource "google_project_iam_member" "api_vm_artifact_reader" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.api_vm.email}"
}

resource "google_project_iam_member" "api_vm_storage_viewer" {
  project = var.project_id
  role    = "roles/storage.objectViewer"
  member  = "serviceAccount:${google_service_account.api_vm.email}"
}

# Grant CI service account SSH + sudo access via OS Login
resource "google_project_iam_member" "ci_os_admin_login" {
  count   = var.ci_service_account_email != null ? 1 : 0
  project = var.project_id
  role    = "roles/compute.osAdminLogin"
  member  = "serviceAccount:${var.ci_service_account_email}"
}

# Grant CI service account IAP tunnel access
resource "google_project_iam_member" "ci_iap_tunnel" {
  count   = var.ci_service_account_email != null ? 1 : 0
  project = var.project_id
  role    = "roles/iap.tunnelResourceAccessor"
  member  = "serviceAccount:${var.ci_service_account_email}"
}

# Grant CI service account ability to look up instances (required for IAP SSH)
resource "google_project_iam_member" "ci_compute_viewer" {
  count   = var.ci_service_account_email != null ? 1 : 0
  project = var.project_id
  role    = "roles/compute.viewer"
  member  = "serviceAccount:${var.ci_service_account_email}"
}

# Allow SSH from IAP (Google's IAP IP range only, not public internet)
resource "google_compute_firewall" "iap_ssh" {
  name    = "${var.api_vm_name}-iap-ssh"
  network = "default"
  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
  target_tags   = var.api_vm_network_tags
  source_ranges = ["35.235.240.0/20"]  # Google IAP range
}

resource "google_compute_firewall" "api_http" {
  name    = "${var.api_vm_name}-http"
  network = "default"
  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }
  target_tags   = var.api_vm_network_tags
  source_ranges = ["0.0.0.0/0"]
}

resource "google_compute_firewall" "api_udp" {
  name    = "${var.api_vm_name}-udp"
  network = "default"
  allow {
    protocol = "udp"
    ports    = ["1024-65535"]
  }
  target_tags   = var.api_vm_network_tags
  source_ranges = ["0.0.0.0/0"]
}

resource "google_compute_instance" "api_vm" {
  name         = var.api_vm_name
  machine_type = var.api_vm_machine_type
  zone         = var.zone
  tags         = var.api_vm_network_tags

  boot_disk {
    initialize_params {
      image = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts"
      size  = var.api_vm_disk_size_gb
    }
  }

  network_interface {
    network = "default"
    access_config {
      nat_ip = google_compute_address.api_ip.address
    }
  }

  metadata = {
    enable-oslogin = "TRUE"
    # Bump this to force VM recreation
    force-recreate = "1"
  }

  lifecycle {
    replace_triggered_by = [null_resource.vm_recreate_trigger]
  }

  metadata_startup_script = templatefile("${path.module}/templates/startup.sh.tmpl", {
    api_image   = var.api_image
    api_port    = var.api_app_port
    api_domain  = "${google_compute_address.api_ip.address}.sslip.io"
    env_content = local.api_env_content
    acme_email  = var.tls_contact_email
  })

  service_account {
    email  = google_service_account.api_vm.email
    scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  depends_on = [
    google_project_service.enabled,
    google_project_iam_member.api_vm_logs_writer,
    google_project_iam_member.api_vm_artifact_reader,
    google_project_iam_member.api_vm_storage_viewer
  ]
}
