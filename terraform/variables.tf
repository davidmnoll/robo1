variable "project_id" {
  description = "GCP project ID where resources will be created"
  type        = string
  default     = "robo1-489405"
}

variable "region" {
  description = "Primary region for Cloud Run and Cloud SQL"
  type        = string
  default     = "us-central1"
}

variable "api_image" {
  description = "Container image reference for the FastAPI gateway (e.g. gcr.io/PROJECT/robot-gateway:latest)"
  type        = string
  default     = "gcr.io/robo1-489405/robot-gateway:latest"
}

variable "cloud_run_service_name" {
  description = "Optional override for the Cloud Run service name"
  type        = string
  default     = null
}

variable "cloud_run_cpu" {
  description = "CPU limit for the Cloud Run container (e.g. 1, 2)"
  type        = string
  default     = "1"
}

variable "cloud_run_memory" {
  description = "Memory limit for the Cloud Run container"
  type        = string
  default     = "1Gi"
}

variable "access_token_expire_minutes" {
  description = "JWT expiration window used by the API"
  type        = number
  default     = 60
}

variable "ros_push_key" {
  description = "Shared secret the ROS camera forwarder will use when POSTing frames"
  type        = string
  default     = "local-dev-key"
}

variable "gateway_name" {
  description = "Logical name for this API deployment"
  type        = string
  default     = "gateway-1"
}

variable "cors_allow_origins" {
  description = "List of allowed browser origins for CORS"
  type        = list(string)
  default     = ["*"]
}

variable "seed_users_json" {
  description = "Optional JSON array that seeds initial API users"
  type        = string
  default     = ""
}

variable "seed_lobbies_json" {
  description = "Optional JSON array that seeds lobbies"
  type        = string
  default     = ""
}

variable "seed_bots_json" {
  description = "Optional JSON array that seeds bots"
  type        = string
  default     = ""
}

variable "cloud_sql_instance_name" {
  description = "Optional Cloud SQL instance name override"
  type        = string
  default     = null
}

variable "db_instance_tier" {
  description = "Machine tier for the Cloud SQL PostgreSQL instance"
  type        = string
  default     = "db-f1-micro"
}

variable "db_disk_size_gb" {
  description = "Disk size (GB) allocated to the Cloud SQL instance"
  type        = number
  default     = 20
}

variable "db_deletion_protection" {
  description = "Whether to protect the Cloud SQL instance from accidental deletion"
  type        = bool
  default     = true
}

variable "db_name" {
  description = "Application database name"
  type        = string
  default     = null
}

variable "db_user" {
  description = "Application database user name"
  type        = string
  default     = null
}
