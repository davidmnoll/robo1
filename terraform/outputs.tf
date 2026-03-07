output "app_engine_url" {
  description = "Public URL for the deployed App Engine service"
  value       = local.app_engine_url
}

output "app_engine_service_name" {
  description = "Name of the App Engine service"
  value       = var.app_engine_service_name
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
