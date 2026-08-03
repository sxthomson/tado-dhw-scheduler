# Technical Specification: Tado DHW Scheduler Cloud-Native Migration

## 1. System Overview & Historical Context
**Application:** Tado Domestic Hot Water (DHW) State Reconciliation Scheduler.

**Purpose:** 
The native Tado ecosystem lacks robust, standalone state reconciliation for DHW. This Python-based application was built to autonomously enforce hot water schedules by polling the current Tado setpoint and pushing corrective API calls if the system state diverges from the desired configuration.

**Historical Challenges Solved (Must be preserved in the new architecture):**
*   **OAuth2 Device Flow:** The system authenticates via Auth0 OpenID Connect (OIDC) (`openid email profile`). 
*   **Token Lifecycles:** The system escapes the strict 8-hour access token expiration boundary by requesting the `offline_access` scope, securing a permanent `refresh_token` that allows autonomous, background re-authentication.
*   **API Throttling:** The system includes internal state tracking to prevent spamming the Tado backend, applying state changes only when necessary and handling rate-limiting gracefully.

## 2. Migration Objectives & Architectural Constraints
The application currently runs as a continuous 24/7 containerized polling loop. The objective is to refactor the application into a modern, event-driven, cloud-native microservice on AWS.

The executing agent must design and provision an architecture adhering to the following core constraints:

1.  **Zero-Cost / Free Tier Optimization:** The architecture must eliminate idle compute. It should natively bypass hourly compute costs and strictly avoid any public IPv4 allocation charges. The agent must select the most appropriate ephemeral/serverless compute and scheduling services to achieve this.
2.  **Best-Practice Secrets Management:** OAuth tokens (`access_token`, `refresh_token`) must be treated as highly sensitive data. The agent must select and implement an AWS-native service designed specifically for secure secrets storage. 
3.  **Decoupled Configuration:** Hot water schedules must be stored in a cloud-native, human-accessible storage medium. A human operator must be able to edit the schedule (e.g., via the AWS Management Console) and have the system instantly recognize the changes on the next execution cycle without requiring a PR, code change, or CI/CD pipeline execution.
4.  **GitOps Deployment:** All Infrastructure as Code (IaC) and application code deployments must be automated via GitHub Actions.

## 3. System Capabilities & Agent Directives

The executing agent is responsible for selecting the optimal AWS services for the job. Please deliver the following artifacts:

### Phase 1: Infrastructure as Code (IaC)
Provide a complete Infrastructure as Code template (e.g., AWS CloudFormation or SAM) that provisions:
*   **Compute & Scheduling:** An ephemeral execution environment triggered on a cron-based schedule (every 1 to 5 minutes).
*   **Configuration Storage:** A decoupled storage layer for the `config.yaml` file.
*   **Secrets Storage:** A secure vault/store for the OAuth tokens.
*   **IAM / Security:** Strict Principle of Least Privilege (PoLP) execution roles. The compute environment must only have access to read the configuration and read/write the specific secrets required.

### Phase 2: Application Refactoring (Python 3.11)
Rewrite the existing application modules (`main.py`, `config_manager.py`, `tado_auth.py`, `tado_client.py`) to conform to the new architecture:
*   **Entry Point:** Convert the continuous `while True` loop into an event-driven handler function.
*   **State & Config Loading:** Refactor `config_manager.py` to fetch the schedule from the chosen AWS storage medium on each invocation, rather than relying on a local file system observer.
*   **Auth Refactoring:** Refactor `tado_auth.py` to securely read/write tokens using the chosen AWS secrets service (e.g., via `boto3`). 
*   **Cold Start Initialization:** Provide a mechanism (or clear instructions) for handling the initial Auth0 Device Authorization Flow (which requires a human to read a code from stdout and enter it into a browser) in a serverless environment where tokens do not yet exist in the vault.

### Phase 3: CI/CD Pipeline
Provide a complete GitHub Actions workflow (`.github/workflows/deploy.yml`) that:
*   Authenticates securely with AWS (e.g., via OIDC).
*   Deploys/Updates the IaC stack.
*   Packages and deploys the refactored Python application code.