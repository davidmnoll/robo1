## Terraform deployment

This directory provisions the managed stack that hosts the FastAPI gateway plus the backing PostgreSQL instance on GCP.

### Resources

- Enables core Google APIs (`run.googleapis.com`, `sqladmin.googleapis.com`, `compute.googleapis.com`, `iam.googleapis.com`)
- Cloud SQL for PostgreSQL 15 (instance + database + user/password)
- Cloud Run service for the API container (public unauthenticated ingress)
- Service account that Cloud Run uses to talk to Cloud SQL

### Prerequisites

1. Create (once) a bucket for Terraform state: `gsutil mb -p <PROJECT_ID> gs://robo1-terraform-state`
2. Build and push the API container image referenced by `var.api_image`
3. Configure application secrets:
   - Set `ros_push_key` or override via `TF_VAR_ros_push_key`
   - Update `cors_allow_origins` to match your GitHub Pages domain
4. Authenticate gcloud / Terraform with a service account that has Project Editor + Cloud SQL Admin permissions (the repo uses the `GCP_TERRAFORM_TOKEN` secret)

### Usage

```
cd terraform
terraform init
terraform plan -var="project_id=robo1-489405" -var="region=us-central1"
terraform apply
```

Outputs include the Cloud Run URL, Cloud SQL connection name, and generated database credentials. These values feed the ROS camera forwarder (`API_PUSH_URL`, `ROS_PUSH_KEY`) and any other consumers (e.g., analytics jobs).
