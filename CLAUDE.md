# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A serverless worker that enforces a Domestic Hot Water (DHW) schedule on a Tado X system by driving the Tado cloud API. It runs as an **AWS Lambda** (Python 3.11, arm64) triggered every 5 minutes by **EventBridge Scheduler**. Schedule and OAuth tokens live in **SSM Parameter Store**. There is no server, container, or idle compute — the design targets $0/month in the AWS free tier. The "interface" is the `/tado/config` SSM parameter (edited in the AWS console) plus CloudWatch logs.

> This replaced an earlier 24/7 EC2 container architecture. If you find references to Docker, EC2, `t4g.nano`, IPv6/NAT64, or ECR, they are stale — those artifacts were removed in the serverless migration.

## Commands

There is no test suite or linter configured. Development/deploy is via the AWS SAM CLI. The full one-time setup/cutover procedure lives in `docs/FIRST_DEPLOY.md`.

```bash
# Validate / build (requires Docker; --use-container builds native wheels for arm64)
sam validate --lint
sam build --use-container

# Deploy manually (CI normally does this on push to main)
sam deploy --stack-name tado-dhw-scheduler --resolve-s3 \
  --no-confirm-changeset --capabilities CAPABILITY_IAM

# One-time OAuth bootstrap (run locally, writes token to SSM SecureString)
pip install boto3 requests
python scripts/bootstrap_auth.py --region eu-west-2

# One-time deploy-role bootstrap (run locally, before the first CI deploy)
aws cloudformation deploy --template-file iac/deploy-role.yaml \
  --stack-name tado-dhw-deploy-role --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides GitHubOrg=<user> GitHubRepo=tado-dhw-scheduler

# Tail logs
aws logs tail /aws/lambda/tado-dhw-scheduler --follow
```

## Architecture

Four application modules under `src/`, imported flat (the Lambda handler is `main.handler`, and SAM packages `src/` as the code root, so imports are `from tado_client import ...`):

- **`main.py`** — Lambda entry point (`handler(event, context)`) plus the reconcile logic. **Stateless and idempotent**: each invocation computes the ruling schedule event via `get_ruling_event`, reads the current DHW setpoint, and only calls the API if they diverge (>0.5°C). This is the key difference from the old design, which kept `last_successful_event_dt` in memory across a `while True` loop — that state can't survive between Lambda invocations, so it was removed. The idempotent compare also corrects manual/external overrides on the next cycle. `_client`/`_config_mgr` are cached in module globals to reuse the `home_id` and boto3 clients across warm invocations. `get_ruling_event` looks back to *yesterday's* last event to cover early-morning hours before today's first event.

- **`tado_auth.py` (`TadoAuthenticator`)** — token load/refresh backed by an **SSM SecureString** (`/tado/token`) via `boto3`, not a local file. `get_valid_token()` is what `TadoClient` calls on every request; it refreshes when expired (60s buffer) and writes the new token back to SSM. **The interactive Device Flow does NOT live here** — in Lambda it raises a clear "run bootstrap_auth.py" error if no token / refresh fails. Note: `refresh_access_token` deliberately **re-attaches the old refresh token** when Tado's response omits a new one — do not "clean this up," it prevents token loss on refresh.

- **`tado_client.py` (`TadoClient`)** — Tado API wrapper (unchanged by the migration). Two hosts: `hops.tado.com` for DHW endpoints, `my.tado.com/api/v2/me` for home discovery. `set_dhw_temperature` clamps to the min/max learned from `setpointConstraints` (defaults 30–65°C). `tenacity` retries network errors; HTTP errors are logged and re-raised.

- **`config_manager.py` (`ConfigManager`)** — fetches the schedule from the **SSM String parameter** (`/tado/config`) on every `load_config()` call and flattens the `schedule:` list into `schedule_map` keyed by 3-letter day, each day sorted by time (the loop depends on this ordering). No `watchdog`, no file I/O, no self-heal — an operator edits the SSM parameter in the console and the next invocation picks it up.

### The Device Flow bootstrap (`scripts/bootstrap_auth.py`)

Runs **locally**, not in AWS. Tado's OAuth Device Flow requires a human to open a URL and approve in a browser, which a scheduled Lambda can't do. The script runs the flow, then `put_parameter`s the token JSON into `/tado/token` as a SecureString. Needs local AWS creds with `ssm:PutParameter`. This is the only manual step after deploy; every scheduled run afterwards is autonomous via the refresh token.

## Infrastructure

- **`template.yaml`** (SAM) — the app stack: the Lambda, its `ScheduleV2` (EventBridge Scheduler) trigger (`rate(5 minutes)`), the `/tado/config` String parameter (seeded with the default schedule), and a least-privilege execution role (`GetParameter` on config+token, `PutParameter` on token only, `kms:Decrypt/Encrypt` scoped via `kms:ViaService = ssm`). **The `/tado/token` SecureString is intentionally NOT in this template** — CloudFormation cannot create SecureString parameters, so it is created by `bootstrap_auth.py`; the IAM policy just references its ARN.

- **`iac/deploy-role.yaml`** — a separate, **one-time** bootstrap stack (deployed by hand, not by CI) that creates the GitHub OIDC provider (optional, via the `ExistingOidcProviderArn` parameter) and the `tado-github-deploy-role` that GitHub Actions assumes. This exists because a deploy role can't deploy itself (chicken-and-egg). Its trust policy is scoped to `repo:<org>/<repo>:*`.

- **`.github/workflows/deploy.yml`** — on push to `main`: OIDC auth (assumes `AWS_DEPLOY_ROLE_ARN`), `sam build --use-container` (QEMU set up first so arm64 wheels build on the x86 runner), `sam deploy --resolve-s3`. Deploy config comes from the `production` GitHub Environment variables `AWS_DEPLOY_ROLE_ARN` and `AWS_REGION`.

## Conventions / gotchas

- **SSM parameter names are the contract**: `/tado/config` (schedule, editable) and `/tado/token` (OAuth, SecureString). Both are configurable via the `CONFIG_PARAM_NAME` / `TOKEN_PARAM_NAME` Lambda env vars (set from the SAM template) and the matching CFN parameters. Change them in one place only.
- The token SecureString is created out-of-band by the bootstrap script, so a fresh deploy has a working Lambda that will error until you run the bootstrap once.
- `config/config.yaml` is now only a **reference/seed** for the SSM parameter's initial value in `template.yaml`; the *live* schedule is the SSM parameter. If you change the schedule format, update both the seed in `template.yaml` and `config_manager.py`.
- Timezone comes from `preferences.timezone` in the config (parsed via stdlib `zoneinfo`; `tzdata` is a dependency so the IANA DB is present on Lambda). The EventBridge schedule also has its own `ScheduleTimezone` — keep them consistent.
- `boto3` is provided by the Lambda runtime and is intentionally **not** in `requirements.txt`; it must be `pip install`ed locally to run the bootstrap script.
- `requirements.txt` must live in `src/` (the SAM `CodeUri`), **not** the repo root. SAM's Python builder only installs a manifest found inside `CodeUri`; a root-level `requirements.txt` is silently ignored and ships a dependency-less package (`Runtime.ImportModuleError: No module named 'requests'`).
- `tado-ec2-key.pem` in the repo root is a leftover private SSH key from the old EC2 setup; it is gitignored (not tracked) and no longer used — safe to delete locally, never commit it.
