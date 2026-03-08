variable "project_id" {
  description = "GCP project ID where resources will be created"
  type        = string
  default     = "robo1-489405"
}

variable "region" {
  description = "Primary region for Compute Engine and Cloud SQL"
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "Compute Engine zone for the API VM"
  type        = string
  default     = "us-central1-a"
}

variable "api_image" {
  description = "Container image reference for the FastAPI gateway (e.g. gcr.io/PROJECT/robot-gateway:latest)"
  type        = string
  default     = "gcr.io/robo1-489405/robot-gateway:latest"
}

variable "api_vm_name" {
  description = "Name of the Compute Engine VM hosting the API"
  type        = string
  default     = "robot-gateway-api"
}

variable "api_vm_machine_type" {
  description = "Compute Engine machine type for the API VM"
  type        = string
  default     = "e2-small"
}

variable "api_vm_disk_size_gb" {
  description = "Boot disk size (GB) for the API VM"
  type        = number
  default     = 30
}

variable "api_vm_network_tags" {
  description = "Network tags applied to the API VM for firewall rules"
  type        = list(string)
  default     = ["robot-gateway"]
}

variable "api_app_port" {
  description = "Port exposed by the FastAPI container (proxied by Caddy)"
  type        = number
  default     = 8080
}

variable "access_token_expire_minutes" {
  description = "JWT expiration window used by the API"
  type        = number
  default     = 60
}

variable "ros_push_key" {
  description = "Shared lobby key the ROS camera forwarder uses when POSTing frames"
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
  default     = ["https://davidmnoll.github.io"]
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
