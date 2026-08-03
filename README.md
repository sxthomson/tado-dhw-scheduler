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
| `docs/FIRST_DEPLOY.md` | Step-by-step first-deploy / cutover runbook |

## First-time setup

The full, step-by-step first deploy (and cutover from the old EC2 version) is in
**[`docs/FIRST_DEPLOY.md`](docs/FIRST_DEPLOY.md)** — it covers creating a
least-privilege bootstrap identity, the deploy role, GitHub variables, deploying
the app, seeding the OAuth token, verification, and cleanup.

> **All AWS steps use a _personal_ AWS account — never a corporate/work account.**
> Before every AWS command, run `aws sts get-caller-identity` and confirm the
> `Account` is your personal account.

At a glance:

1. **Bootstrap identity** — create an IAM user (or SSO login) scoped by
   `iac/bootstrap-user-policy.json`.
2. **Deploy role** — `aws cloudformation deploy … iac/deploy-role.yaml`.
3. **GitHub vars** — set `AWS_DEPLOY_ROLE_ARN` + `AWS_REGION` in the `production`
   environment.
4. **Deploy the app** — push to `main`; CI builds and deploys the SAM stack.
5. **Seed the token** — `python scripts/bootstrap_auth.py --region <region>`
   (one-time browser approval).
6. **Verify**, then tear down the old EC2 stack.

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
the serverless stack is deployed and authenticated, delete the old CloudFormation
stack in the AWS console to stop the EC2 charges. That old stack also owned the
previous OIDC provider and deploy role, so deleting it removes them — the
deploy-role template's `ExistingOidcProviderArn` parameter handles the collision
if you deploy the new role before deleting the old stack. Full sequence and
caveats are in [`docs/FIRST_DEPLOY.md`](docs/FIRST_DEPLOY.md).
