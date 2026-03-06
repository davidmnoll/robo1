
PROJECT_ID="robo1-489405"
SA_NAME="terraform"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

create-service-account: 

	# 1. Create the service account
	gcloud iam service-accounts create "${SA_NAME}" \
	--display-name "Terraform automation" \
	--project "${PROJECT_ID}"

	# 2. Grant it the roles Terraform will need (Editor + SA User as a baseline)
	gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
	--member "serviceAccount:${SA_EMAIL}" \
	--role "roles/editor"

	gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
	--member "serviceAccount:${SA_EMAIL}" \
	--role "roles/iam.serviceAccountUser"

	# After that, generate a key (so we can upload it as GCP_TERRAFORM_TOKEN):

create-service-acct-key:
	gcloud iam service-accounts keys create /tmp/robo1-terraform.json \
	--iam-account="${SA_EMAIL}" \
	--project "${PROJECT_ID}"

upload-service-acct-key:
	gh secret set GCP_TERRAFORM_TOKEN < /tmp/robo1-terraform.json

enable-pages: 
	gh api repos/davidmnoll/robo1/pages --method PUT \
      -F source[branch]=main \
      -F source[path]=/
