# Tado DHW Scheduler (Serverless)

A background reconciler that enforces a Domestic Hot Water (DHW) schedule on a
Tado X system, ensuring hot-water priority for 4-pipe boilers (e.g. Viessmann
Vitodens 100-W).

It runs as an **AWS Lambda** triggered every 5 minutes by **EventBridge
Scheduler**. There is no idle compute and no public IPv4 — the whole thing sits
comfortably in the AWS free tier. Schedule and OAuth tokens live in **SSM
Parameter Store**, so the schedule is editable from the AWS console without a
code change or redeploy.

## Architecture

```
EventBridge Scheduler (rate: 5 min)
        │ invoke
        ▼
   Lambda  (Python 3.11, arm64)   ── stateless, idempotent reconcile
        ├─ GetParameter  /tado/config   (SSM String)       ← edit schedule in console
        ├─ GetParameter  /tado/token    (SSM SecureString)  ← read OAuth token
        ├─ PutParameter  /tado/token    (SSM SecureString)  ← write back on refresh
        └─ HTTPS → Tado API
```

Each invocation is **idempotent**: it computes the schedule event that should
be active now, reads the current DHW setpoint, and only pushes a change if they
diverge (>0.5 °C). No cross-invocation state is kept, and any manual/external
override is corrected on the next cycle.

Preserved from the original design: OAuth2 Device Flow auth, the `offline_access`
refresh-token lifecycle (survives the 8-hour access-token expiry), and API
throttle-avoidance (only writes on divergence).

## Repository layout

| Path | Purpose |
|------|---------|
| `src/main.py` | Lambda handler + reconciliation logic |
| `src/config_manager.py` | Loads schedule from the SSM config parameter |
| `src/tado_auth.py` | OAuth token load/refresh backed by the SSM SecureString |
| `src/tado_client.py` | Tado API client (setpoint get/set, retries, clamping) |
| `scripts/bootstrap_auth.py` | One-time local OAuth device-flow bootstrap |
| `template.yaml` | SAM stack (Lambda, schedule, config param, IAM) |
| `iac/deploy-role.yaml` | One-time bootstrap of the GitHub Actions deploy role |
| `iac/bootstrap-user-policy.json` | Least-privilege policy for the local operator who runs the one-time bootstrap |
| `.github/workflows/deploy.yml` | CI: build + deploy the SAM stack on push to `main` |
| `config/config.yaml` | Reference copy of the schedule (seeds the SSM param) |

## First-time setup

> **All AWS steps below use a _personal_ AWS account — never a corporate/work
> account.** Before every AWS command, run `aws sts get-caller-identity` and
> confirm the `Account` is your personal account.

### 0. Create the bootstrap operator credentials (once)

CI uses short-lived OIDC credentials and needs no keys. But the *one-time*
steps you run by hand (step 1 below, and seeding the token in step 4) need
local AWS credentials. Rather than root keys or a broad admin user, create a
dedicated IAM user scoped to exactly those actions using
`iac/bootstrap-user-policy.json`.

**Preferred: IAM Identity Center (SSO)** — no long-lived keys on your machine:

```bash
aws configure sso --profile tado-personal
aws sso login --profile tado-personal
export AWS_PROFILE=tado-personal
```
Attach the policy below to the permission set / user you log in as.

**Alternative: a dedicated IAM user** (console — *IAM → Users → Create user*):

1. Create a user, e.g. `tado-bootstrap`. Do **not** give it console access unless you want it.
2. **Enable MFA** on the user (*Security credentials → Assign MFA device*).
3. Attach an inline policy from `iac/bootstrap-user-policy.json`, first replacing
   `<ACCOUNT_ID>` and `<REGION>` with your personal account id and region.
   Via CLI (run by an existing admin, or from the console's JSON editor):
   ```bash
   ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
   REGION=eu-west-2
   sed "s/<ACCOUNT_ID>/$ACCOUNT_ID/g; s/<REGION>/$REGION/g" \
     iac/bootstrap-user-policy.json > /tmp/tado-bootstrap-policy.json
   aws iam put-user-policy --user-name tado-bootstrap \
     --policy-name tado-bootstrap --policy-document file:///tmp/tado-bootstrap-policy.json
   ```
4. Create access keys (*Security credentials → Create access key → CLI*) and
   store them in a named profile:
   ```bash
   aws configure --profile tado-personal   # paste the key id / secret / region
   export AWS_PROFILE=tado-personal
   ```
5. **After the bootstrap is complete** (through step 4), delete these access keys
   — ongoing deploys run via CI/OIDC and don't need them. Keep the user (with no
   active keys) so re-runs are easy: create a fresh key, run, delete again.

What the policy allows, and nothing more: deploy/update/delete only the
`tado-dhw-deploy-role` CloudFormation stack; manage only the
`tado-github-deploy-role` IAM role and the GitHub OIDC provider; and
`PutParameter` only on `/tado/token` (plus the KMS encrypt needed for a
SecureString, scoped to SSM).

### 1. Create the GitHub Actions deploy role (once, from your laptop)

Uses the bootstrap credentials from step 0.

```bash
aws cloudformation deploy \
  --template-file iac/deploy-role.yaml \
  --stack-name tado-dhw-deploy-role \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides GitHubOrg=<your-gh-username> GitHubRepo=tado-dhw-scheduler
```

If your account already has a GitHub OIDC provider (e.g. from the old EC2
stack), add `ExistingOidcProviderArn=arn:aws:iam::<acct>:oidc-provider/token.actions.githubusercontent.com`
to `--parameter-overrides` so it reuses the existing one.

Grab the role ARN from the stack outputs:

```bash
aws cloudformation describe-stacks --stack-name tado-dhw-deploy-role \
  --query "Stacks[0].Outputs[?OutputKey=='DeployRoleArn'].OutputValue" --output text
```

### 2. Configure GitHub

In the repo: **Settings → Environments → `production`**, add **variables**:

| Variable | Value |
|----------|-------|
| `AWS_DEPLOY_ROLE_ARN` | the role ARN from step 1 |
| `AWS_REGION` | e.g. `eu-west-2` |

### 3. Deploy the app

Push to `main` (or run the `Deploy Tado DHW Scheduler` workflow manually). This
creates the Lambda, the 5-minute schedule, and the `/tado/config` parameter.

### 4. Seed the OAuth token (once, from your laptop)

The Tado Device Flow needs a human to approve access in a browser, so it can't
run in Lambda. Run it locally — it writes the token into SSM where the Lambda
reads it:

```bash
pip install boto3 requests
python scripts/bootstrap_auth.py --region eu-west-2
```

Open the printed URL, approve, done. Every scheduled run afterwards is
autonomous via the stored refresh token.

## Operations

**Change the schedule** — edit the `/tado/config` parameter in the AWS console
(*Systems Manager → Parameter Store*). The next run (≤5 min) picks it up. No PR,
no deploy. Keep `config/config.yaml` in sync if you want the repo to reflect the
live schedule.

**Re-authenticate** — if the refresh token is ever revoked, re-run
`scripts/bootstrap_auth.py`.

**Logs** — CloudWatch Logs group `/aws/lambda/tado-dhw-scheduler`. Each run logs
whether it was a no-op or applied a change.

**Run cadence / timezone** — override the `ScheduleExpression` and
`ScheduleTimezone` stack parameters in `template.yaml` (defaults: `rate(5 minutes)`,
`Europe/London`).

## Migrating from the old EC2 architecture

This project previously ran a 24/7 container on an EC2 `t4g.nano` instance. Once
the serverless stack is deployed and authenticated (steps above), delete the old
CloudFormation stack in the AWS console to stop the EC2 charges. That old stack
also owned the previous OIDC provider and deploy role — see step 1 for handling
the OIDC provider so the two don't collide.
