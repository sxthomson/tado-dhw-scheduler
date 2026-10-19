# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A serverless worker that enforces a Domestic Hot Water (DHW) schedule on a Tado X system by driving the Tado cloud API. It runs as an **AWS Lambda** (Python 3.11, arm64) triggered every 5 minutes by **EventBridge Scheduler**. Schedule and OAuth tokens live in **SSM Parameter Store**. There is no server, container, or idle compute — the design targets $0/month in the AWS free tier. The "interface" is the `/tado/config` SSM parameter (edited in the AWS console) plus CloudWatch logs.

It also (optionally, via config) drives Tado's **Maximum Flow Temperature slider** from the outdoor temperature — a "meta-slider" over Tado's own weather auto-adaptation — and logs **room telemetry** used to tune that curve and to judge which radiators are undersized.

> This replaced an earlier 24/7 EC2 container architecture. If you find references to Docker, EC2, `t4g.nano`, IPv6/NAT64, or ECR, they are stale — those artifacts were removed in the serverless migration.

## Commands

Development/deploy is via the AWS SAM CLI. The full one-time setup/cutover procedure lives in `docs/FIRST_DEPLOY.md`. Tests are plain pytest with fakes (no AWS, no network); there's no linter and CI doesn't run the tests.

```bash
# Tests (fakes only; tests/conftest.py puts src/ and scripts/ on sys.path)
pip install -r requirements-dev.txt -r src/requirements.txt boto3
pytest -q

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

# Tail logs (TADO_TELEMETRY_V1 = one slow-loop record per 15 min)
aws logs tail /aws/lambda/tado-dhw-scheduler --follow
aws logs tail /aws/lambda/tado-dhw-scheduler --filter-pattern TADO_TELEMETRY_V1

# Flow-curve / radiator report from those records (local, personal AWS creds)
python scripts/flow_report.py --region eu-west-2 --days 14
```

## Architecture

Six application modules under `src/`, imported flat (the Lambda handler is `main.handler`, and SAM packages `src/` as the code root, so imports are `from tado_client import ...`):

- **`main.py`** — Lambda entry point (`handler(event, context)`) plus the reconcile logic. **Idempotent**: each invocation computes the ruling schedule event via `get_ruling_event`, reads the current DHW setpoint, and only calls the API if they diverge (>0.5°C, `TOLERANCE_C`). This is the key difference from the old design, which kept `last_successful_event_dt` in memory across a `while True` loop — that state can't survive between Lambda invocations, so it was removed. **Manual boosts via the Tado app are deliberately NOT corrected** — `reconcile()` uses `OverrideTracker` (one small SSM String param, `/tado/last_applied`) to tell "the schedule moved on since our last write" apart from "something else changed it since our last write" (a boost), and respects the latter for `preferences.override_grace_minutes` (default 60) before reasserting control. This exists because the Tado API (`hops.tado.com` DHW endpoints) was confirmed by live probing to expose **no** overlay/termination/"who set this" metadata — a boost and our own scheduled write are indistinguishable at the API level, so the last-applied-memory workaround is intentional, not a placeholder for a "proper" overlay-based fix. `_client`/`_config_mgr`/`_tracker`/`_flow_tracker` are cached in module globals to reuse the `home_id` and boto3 clients across warm invocations. `get_ruling_event` looks back to *yesterday's* last event to cover early-morning hours before today's first event.
  **`handler`** loads the config once, then runs DHW `reconcile()` every tick and the **slow loop** (`run_slow_loop`: flow ceiling + telemetry) on one tick per 15-minute slot, each in its own try/except — either failing still lets the other run, and the invocation then raises one combined error so it shows as a Lambda error. The slot gate (`is_slow_tick`) uses `event["scheduled_time"]` (EventBridge Scheduler's exact `<aws.scheduler.scheduled-time>`, passed via the schedule's `Input`) rather than the wall clock, so start-up jitter can't produce two slow runs (or none) in a slot; it assumes 5-minute ticks (`TICK_SECONDS`). The slow loop fetches weather, flow settings and (if telemetry is on) rooms + roomsAndDevices once, each isolated, then hands the flow/weather payloads to `reconcile_flow` and emits **one** `TADO_TELEMETRY_V1 {json}` log line (from a `finally`, so failures are recorded too). Room-fetch failures are logged only; flow failures raise.

- **`flow_reconciler.py`** — the max flow "meta-slider". Tado X exposes only one knob, `maxFlowTemperature` (the app slider, 20–82); with `autoAdaptation.enabled`, Tado derives the cap it *actually* applies (`autoAdaptation.maxFlowTemperature`, the "effective" cap) by scaling the slider with its own weather curve. **Probed 2026-10-19 at 9.8 °C outdoor: effective = 0.8 × slider + 3, exactly, for sliders 55–75** — the slider scales the curve rather than capping it, so raising it for winter also raises mild-weather flow; that's the problem this solves. The config curve is therefore in **slider** units, not flow temperatures. `parse_flow_config` → `FlowConfig` (None when the block is absent/disabled; `ValueError` on a bad curve); `interpolate` is piecewise-linear, flat beyond the ends, rounded half-up (not `round()`, which rounds halves to even); `decide` is pure and mirrors the DHW override logic with **rule 1 = "live value within `min_change_c` of target → adopt it as ours, no write"** first, which also makes a crash between PATCH and save harmless. Then: no memory → bootstrap apply; live == last write → follow the curve; otherwise a manual change in the Tado app → respect it for `override_grace_minutes` (restarting if it changes again). `reconcile_flow` skips (never re-enables) when auto-adaptation is off — then the slider *is* the boiler cap and the curve would run far too hot — and skips on a weather reading older than 180 min. On apply it PATCHes, **saves the tracker before verifying**, re-reads after 2 s and checks the slider (the effective value reads `null` for ~15–30 s after any slider change while Tado recomputes — never treat that as an error), restoring the old tracker state and raising if the write didn't land. `dry_run: true` reads the tracker (so logged decisions are realistic) but never PATCHes or saves.

- **`telemetry.py`** — shapes the slow-loop record: `summarise()` (outdoor °C + timestamp, solar %, slider, effective, auto) and `project_rooms()` (compact per-room keys: `t` inside temp, `h` humidity, `sp` setpoint or null when OFF, `pw` valve heatingPower %, `win`/`man`/`boost`/`away` flags, `dev` device types — `SU04` room sensor / `VA04` valve; valve-only rooms read temp next to the radiator — and `dis` = anything not CONNECTED). `emit()` logs `TADO_TELEMETRY_V1 {json}`; `scripts/flow_report.py` parses exactly that, so bump the marker/`SCHEMA_VERSION` if the shape changes incompatibly.

- **`override_tracker.py`** (`OverrideTracker`) — thin wrapper around an SSM **String** parameter (`/tado/last_applied`, `LAST_APPLIED_PARAM_NAME` env var; a second instance uses `/tado/flow_last_applied`, `FLOW_LAST_APPLIED_PARAM_NAME`, for the flow slider) holding `{last_applied_temp, last_applied_at, override_since?, override_temp?}`. This is reconciler-owned operational state, not user config — never hand-edit it (safe to delete/reset to `{}` though; worst case is one extra "bootstrap" apply on the next run). `load()` treats a missing/corrupt parameter as `{}` rather than raising, so a fresh stack (seeded to `{}` by `template.yaml`) just bootstraps normally.

- **`tado_auth.py` (`TadoAuthenticator`)** — token load/refresh backed by an **SSM SecureString** (`/tado/token`) via `boto3`, not a local file. `get_valid_token()` is what `TadoClient` calls on every request; it refreshes when expired (60s buffer) and writes the new token back to SSM. **The interactive Device Flow does NOT live here** — in Lambda it raises a clear "run bootstrap_auth.py" error if no token / refresh fails. Note: `refresh_access_token` deliberately **re-attaches the old refresh token** when Tado's response omits a new one — do not "clean this up," it prevents token loss on refresh.

- **`tado_client.py` (`TadoClient`)** — Tado API wrapper. Two hosts: `hops.tado.com` for DHW, flow settings (`settings/flowTemperatureOptimization`: GET, and PATCH `{"maxFlowTemperature": int}`) and rooms (`rooms`, `roomsAndDevices`); `my.tado.com/api/v2` for home discovery (`/me`) and weather (`/homes/{id}/weather` — hops has no weather endpoint). `set_dhw_temperature` clamps to the min/max learned from `setpointConstraints` (defaults 30–65°C); `set_max_flow_temperature` clamps to `flow_min`/`flow_max` learned from `maxFlowTemperatureConstraints` and **refuses to write until those have been read** (they're deliberately separate from the DHW limits). Every request has a `(5, 10)` s connect/read timeout. `tenacity` retries connection errors on writes and discovery; the new GETs don't retry (the next slow-loop slot is the retry). HTTP errors are logged and re-raised.

- **`config_manager.py` (`ConfigManager`)** — fetches the schedule from the **SSM String parameter** (`/tado/config`) on every `load_config()` call and flattens the `schedule:` list into `schedule_map` keyed by 3-letter day, each day sorted by time (the loop depends on this ordering). No `watchdog`, no file I/O, no self-heal — an operator edits the SSM parameter in the console and the next invocation picks it up.

### The Device Flow bootstrap (`scripts/bootstrap_auth.py`)

Runs **locally**, not in AWS. Tado's OAuth Device Flow requires a human to open a URL and approve in a browser, which a scheduled Lambda can't do. The script runs the flow, then `put_parameter`s the token JSON into `/tado/token` as a SecureString. Needs local AWS creds with `ssm:PutParameter`. This is the only manual step after deploy; every scheduled run afterwards is autonomous via the refresh token.

## Infrastructure

- **`template.yaml`** (SAM) — the app stack: the Lambda, its `ScheduleV2` (EventBridge Scheduler) trigger (`rate(5 minutes)`, with `Input` carrying `<aws.scheduler.scheduled-time>` for the slow-loop gate), the `/tado/config` String parameter (seeded with the default schedule), the `/tado/last_applied` and `/tado/flow_last_applied` String parameters (seeded to `{}`), and a least-privilege execution role (`GetParameter` on config+token+both trackers, `PutParameter` on token and both trackers, `kms:Decrypt/Encrypt` scoped via `kms:ViaService = ssm`). The function has `Timeout: 120` and `EventInvokeConfig.MaximumRetryAttempts: 0` — Scheduler invokes asynchronously, and Lambda's default 2 retries could overlap the next tick and race the OAuth refresh; every tick is an idempotent reconcile, so the next one is the retry. **The `/tado/token` SecureString is intentionally NOT in this template** — CloudFormation cannot create SecureString parameters, so it is created by `bootstrap_auth.py`; the IAM policy just references its ARN. (`/tado/last_applied` is a plain String, so unlike the token it *is* created here.)

- **`iac/deploy-role.yaml`** — a separate, **one-time** bootstrap stack (deployed by hand, not by CI) that creates the GitHub OIDC provider (optional, via the `ExistingOidcProviderArn` parameter) and the `tado-github-deploy-role` that GitHub Actions assumes. This exists because a deploy role can't deploy itself (chicken-and-egg). Its trust policy is scoped to `repo:<org>/<repo>:*`.

- **`.github/workflows/deploy.yml`** — on push to `main`: OIDC auth (assumes `AWS_DEPLOY_ROLE_ARN`), `sam build --use-container` (QEMU set up first so arm64 wheels build on the x86 runner), `sam deploy --resolve-s3`. Deploy config comes from the `production` GitHub Environment variables `AWS_DEPLOY_ROLE_ARN` and `AWS_REGION`.

## Conventions / gotchas

- **SSM parameter names are the contract**: `/tado/config` (schedule + optional `flow_temperature`/`telemetry` blocks, editable), `/tado/token` (OAuth, SecureString), `/tado/last_applied` (DHW override-tracking state, reconciler-owned) and `/tado/flow_last_applied` (flow slider override-tracking state, reconciler-owned). All four are configurable via the `CONFIG_PARAM_NAME` / `TOKEN_PARAM_NAME` / `LAST_APPLIED_PARAM_NAME` / `FLOW_LAST_APPLIED_PARAM_NAME` Lambda env vars (set from the SAM template) and the matching CFN parameters. Change them in one place only. The two trackers must never share a parameter — `OverrideTracker.save()` replaces the whole object — and `OverrideTracker(param_name=None)` falls back to the DHW one, so always pass the flow name explicitly.
- **Editing the template's `ConfigParameter` overwrites the live `/tado/config`.** CloudFormation rewrites the parameter whenever *any* of its properties change (even `Description`), discarding console edits. The seed (and `config/config.yaml`) were last synced to the live schedule on 2026-10-19; the `flow_temperature`/`telemetry` blocks are deliberately **not** in the seed — they're added to the live parameter by hand, *after* any deploy that changes the seed. Don't touch `ConfigParameter` without first copying the live value into it.
- **Flow config (`flow_temperature:` in `/tado/config`)**: `enabled`, `dry_run`, `curve` (list of `{outdoor_c, slider_c}`), `min_change_c` (deadband, default 2), `override_grace_minutes` (default 720). Missing block or `enabled: false` = off, DHW unaffected; `telemetry: {enabled: true}` turns on room logging independently. To end a manual slider override early (or after editing the curve mid-override), reset `/tado/flow_last_applied` to `{}`. Tune the curve from `scripts/flow_report.py` output, not by moving the app slider (that's treated as a 12 h override).
- Tado API budget: `RateLimit-Policy` headers report **20,000 requests/day** for this account; DHW (~1/tick) + slow loop (~4 per 15 min) is ~700/day.
- **Boosting hot water**: intentionally just "use the Tado app." Don't add a second boost mechanism (a Lambda Function URL, a CLI flag, etc.) without a real reason — the whole point of the override-tracker design was to make the *existing* Tado app control work, not to add a new one.
- The token SecureString is created out-of-band by the bootstrap script, so a fresh deploy has a working Lambda that will error until you run the bootstrap once.
- `config/config.yaml` is now only a **reference/seed** for the SSM parameter's initial value in `template.yaml`; the *live* schedule is the SSM parameter. If you change the schedule format, update both the seed in `template.yaml` and `config_manager.py`.
- Timezone comes from `preferences.timezone` in the config (parsed via stdlib `zoneinfo`; `tzdata` is a dependency so the IANA DB is present on Lambda). The EventBridge schedule also has its own `ScheduleTimezone` — keep them consistent.
- `boto3` is provided by the Lambda runtime and is intentionally **not** in `requirements.txt`; it must be `pip install`ed locally to run the bootstrap script.
- `requirements.txt` must live in `src/` (the SAM `CodeUri`), **not** the repo root. SAM's Python builder only installs a manifest found inside `CodeUri`; a root-level `requirements.txt` is silently ignored and ships a dependency-less package (`Runtime.ImportModuleError: No module named 'requests'`).
- `cfn-lint` (via `sam validate --lint`) flags **E2533: python3.11 deprecated** with block-update dates in the past. It's pre-existing and, as of the 2026-09-27 deploy, AWS still accepted function updates on python3.11 — but plan a runtime upgrade (the code and tests also pass on 3.9 and 3.14).
- The GitHub repo is **public**: keep real room names, people's names, serials and home ids out of committed code, tests and docs (CloudWatch logs and the gitignored `.probe/` are fine).
- `tado-ec2-key.pem` in the repo root is a leftover private SSH key from the old EC2 setup; it is gitignored (not tracked) and no longer used — safe to delete locally, never commit it.
