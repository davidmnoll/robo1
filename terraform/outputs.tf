output "api_url" {
  description = "Public URL for the API endpoint"
  value       = var.environment == "dev" ? "http://${google_compute_address.api_ip.address}:8080" : "https://${google_compute_address.api_ip.address}.sslip.io"
}

output "api_vm_ip" {
  description = "Static external IP assigned to the API VM"
  value       = google_compute_address.api_ip.address
}

output "api_vm_name" {
  description = "Name of the Compute Engine VM hosting the API"
  value       = local.vm_name
}

output "frontend_url" {
  description = "URL for the frontend (GCS bucket in dev, empty in prod)"
  value       = var.environment == "dev" ? "https://storage.googleapis.com/${google_storage_bucket.frontend[0].name}" : ""
}

output "api_vm_zone" {
  description = "Zone where the API VM resides"
  value       = var.zone
}

output "db_instance_connection_name" {
  description = "Cloud SQL connection string for use with Cloud SQL Proxy / connectors"
  value       = local.cloud_sql_connection_name
}

output "db_app_user" {
  description = "Database user configured for the API"
  value       = local.db_user
}

output "db_app_password" {
  description = "Password associated with the API database user"
  value       = random_password.db_password.result
  sensitive   = true
}
