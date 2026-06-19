Here is the complete, consolidated `README.md` for your repository. It reflects the finalized architecture, including the IPv6-only Graviton instance, dynamic SSM tagging, and the headless Tado authentication flow.

---

# Tado DHW Scheduler (AWS / Graviton / IPv6)

A programmatic background worker designed to strictly manage the Domestic Hot Water (DHW) schedule for Tado X, ensuring consistent hot water priority for 4-pipe system boilers (like the Viessmann Vitodens 100-W).

This application is fully containerized and automated via a self-healing GitHub Actions CI/CD pipeline. To minimize infrastructure costs, it runs on an AWS Graviton2 (`t4g.nano`) EC2 instance utilizing an IPv6-only secure ingress setup, bypassing AWS public IPv4 charges.

## 🏗️ Architecture Overview

* **Compute:** AWS EC2 `t4g.nano` (ARM64 / Graviton)
* **Networking:** IPv6 ingress for SSH, NAT64 egress for IPv4 API bridging.
* **Deployment:** GitHub Actions via AWS Systems Manager (SSM) — **No SSH ports exposed to the CI pipeline.**
* **Registry:** Amazon ECR (Elastic Container Registry)
* **State Management:** Persistent host volume mapping for OAuth tokens to survive container destruction.

---

## 🚀 Deployment Guide (From Scratch)

### Step 1: Provision the AWS Infrastructure

The entire environment is defined as Infrastructure as Code.

1. Log into your AWS Console and navigate to **CloudFormation**.
2. Create a new stack using the provided `infrastructure.yaml` file.
3. You will be prompted to enter the following Parameters:
* **`KeyName`:** An existing EC2 Key Pair for manual SSH access.
* **`MyIP`:** Your local broadband/network IPv6 address (e.g., `2a00:1f18:xxxx::/128`) to lock down SSH access.
* **`InstanceType`:** Leave as `t4g.nano`.
* **`GitHubUserName` / `GitHubRepoName`:** Used to scope the OIDC security role strictly to your repository.
* **`InstanceTargetTag`:** Leave as `Tado-DHW-Scheduler` (or your preferred identifier).


4. Acknowledge IAM resource creation and deploy the stack.

### Step 2: Configure GitHub Actions

We use GitHub Environments to securely store variables and deploy without hardcoded infrastructure IDs.

1. Go to your GitHub Repository **Settings** > **Environments** and create an environment named `production`.
2. Add the following **Environment variables**:
* `AWS_ACCOUNT_ID`: Your 12-digit AWS account number (e.g., `123456789012`). *This is used to dynamically construct your secure OIDC Role ARN.*
* `AWS_REGION`: `eu-west-2` (or your chosen region).
* `ECR_REPOSITORY`: `tado-dhw-scheduler`
* `CONTAINER_NAME`: `tado-worker`
* `EC2_TARGET_TAG`: `Tado-DHW-Scheduler` *(Must exactly match the CloudFormation parameter)*



### Step 3: Trigger the First Deployment

With the AWS stack running and GitHub variables set, trigger the CI/CD pipeline:

1. Commit and push your code to the `main` branch.
2. The GitHub Action will:
* Authenticate securely with AWS via OIDC.
* Cross-compile the Python application for ARM64 architecture.
* Push the image to Amazon ECR.
* Instruct AWS SSM to dynamically find your tagged EC2 instance and pull/run the new container.



### Step 4: First-Time Authentication (Tado OAuth)

Because Tado uses a headless OAuth flow, you must manually authorize the device the very first time the container boots. The token is mapped to persistent host storage and will survive all future deployments.

1. SSH into your newly provisioned EC2 instance using its IPv6 address:
`ssh -i key.pem ec2-user@[Your-IPv6-Address]`
2. Check the logs of the running container:
`docker logs tado-worker`
3. You will see a prompt indicating action is required:
`👉 ACTION REQUIRED: Visit this URL to approve access...`
4. Click the link provided in the logs and approve the integration.
5. The application will detect the approval, securely save the `refresh_token` to the host directory `/home/ec2-user/tado-storage`, and immediately begin the scheduling loop.

---

## 🛠️ Maintenance & Operations

**Self-Healing Deployments**
If you ever destroy the CloudFormation stack and rebuild it, the pipeline will automatically heal. The new EC2 instance will boot up with the `Tado-DHW-Scheduler` tag, and the GitHub Action will find it dynamically via SSM. You do not need to update any hardcoded IP addresses or Instance IDs.

**Updating the Schedule**
The DHW schedule is driven by the YAML configuration. To change your heating blocks, edit the configuration file and push to `main`. The container will rebuild and restart automatically.

**Manual Restarts**
If you need to manually intervene without pushing code, connect via IPv6 SSH and execute:

```bash
docker restart tado-worker

```