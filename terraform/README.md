## Terraform deployment

This directory provisions the managed stack that hosts the FastAPI gateway plus the backing PostgreSQL instance on GCP.

### Resources

- Enables core Google APIs (App Engine Flex, Cloud SQL, IAM, Artifact Registry)
- Cloud SQL for PostgreSQL 15 (instance + database + user/password)
- App Engine Flexible service for the API container (public ingress, UDP-capable)
- Service account that App Engine uses to talk to Cloud SQL

### Prerequisites

1. Create (once) a bucket for Terraform state: `gsutil mb -p <PROJECT_ID> gs://robo1-terraform-state`
2. GitHub Actions automatically builds/pushes the API image to `gcr.io/${PROJECT_ID}/robot-gateway:${GITHUB_SHA}` before Terraform runs; if you apply locally, run `gcloud builds submit --tag gcr.io/${PROJECT_ID}/robot-gateway:$(git rev-parse HEAD) ./api` yourself.
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

Outputs include the App Engine URL, Cloud SQL connection name, and generated database credentials. These values feed the ROS camera forwarder (API base URL + lobby key) and any other consumers (e.g., analytics jobs).
