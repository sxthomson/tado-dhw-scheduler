# Tado DHW Scheduler (Serverless)

A background reconciler that enforces a Domestic Hot Water (DHW) schedule on a
Tado X system, ensuring hot-water priority for 4-pipe boilers (e.g. Viessmann
Vitodens 100-W).

It runs as an **AWS Lambda** triggered every 5 minutes by **EventBridge
Scheduler**. There is no idle compute and no public IPv4 — the whole thing sits
comfortably in the AWS free tier.

Optionally it also drives Tado's **Maximum Flow Temperature** slider from the
outdoor temperature (lower in mild weather, higher near freezing — Tado's own
slider shifts its whole weather curve, so a single setting can't do both), and
logs per-room telemetry for tuning that curve and spotting undersized radiators. Schedule and OAuth tokens live in **SSM
Parameter Store**, so the schedule is editable from the AWS console without a
code change or redeploy.

## Architecture

```
EventBridge Scheduler (rate: 5 min)
        │ invoke
        ▼
   Lambda  (Python 3.11, arm64)   ── idempotent reconcile + override tracking
        ├─ GetParameter  /tado/config        (SSM String)       ← edit schedule in console
        ├─ GetParameter  /tado/token         (SSM SecureString)  ← read OAuth token
        ├─ PutParameter  /tado/token         (SSM SecureString)  ← write back on refresh
        ├─ GetParameter  /tado/last_applied  (SSM String)       ← what we set last time
        ├─ PutParameter  /tado/last_applied  (SSM String)       ← record every write
        ├─ Get/PutParameter /tado/flow_last_applied (SSM String) ← same, for the flow slider
        ├─ HTTPS → Tado API                                     ← DHW every tick; weather,
        │                                                          flow + rooms every 15 min
        └─ CloudWatch Logs  TADO_TELEMETRY_V1 {json}            ← one record per 15 min
```

Each invocation is **idempotent**: it computes the schedule event that should
be active now, reads the current DHW setpoint, and only pushes a change if they
diverge (>0.5 °C). The only cross-invocation state is one small SSM parameter
(`/tado/last_applied`) recording the setpoint the Lambda itself last wrote —
just enough to tell "the schedule moved on since our last write" apart from
"something else changed it since our last write" (i.e. a manual boost via the
Tado app). **Boosting hot water**: just use the Tado app as normal — the
reconciler detects that the setpoint no longer matches what it last set, and
leaves it alone for `override_grace_minutes` (default 60, tunable in
`preferences:`) before reasserting the schedule. Tado's API gives no explicit
"this was a manual override" signal (confirmed by probing the live API before
building this — see git history on this section for the investigation), so
this last-applied-memory approach is a deliberate fallback rather than reading
an official overlay/termination field.

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
| `src/override_tracker.py` | Remembers the last setpoint we wrote (SSM String), to detect manual boosts |
| `src/flow_reconciler.py` | Outdoor temperature → max flow slider curve, deadband, manual-override handling |
| `src/telemetry.py` | Compact per-room/home snapshot logged every 15 min |
| `scripts/flow_report.py` | Local report over the telemetry: Tado's curve by outdoor band, per-room radiator verdicts |
| `tests/` | pytest suite (fakes only, no AWS/network) |
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

**Boost hot water on demand** — just use the manual/boost control in the Tado
app, as normal. The next reconcile cycle notices the setpoint no longer
matches what it last wrote, and leaves it alone for `override_grace_minutes`
(default 60 min) before resuming the schedule. No app on your side, no second
"boost" button — the Tado app's own control is the trigger. Tune the grace
window via `override_grace_minutes` in `preferences:` in the `/tado/config`
parameter (same place as the schedule, no redeploy needed). Don't hand-edit
`/tado/last_applied` — it's reconciler-owned bookkeeping, not config.

**Change the schedule** — edit the `/tado/config` parameter in the AWS console
(*Systems Manager → Parameter Store*). The next run (≤5 min) picks it up. No PR,
no deploy. Keep `config/config.yaml` in sync if you want the repo to reflect the
live schedule.

**Weather-driven max flow temperature** — off unless enabled. Add to the
`/tado/config` parameter in the console (not the template — see `CLAUDE.md`):

```yaml
telemetry:
  enabled: true
flow_temperature:
  enabled: true
  dry_run: true          # log decisions only; set false once they look right
  curve:                 # outdoor °C -> app slider °C, linear between points
    - { outdoor_c: 10, slider_c: 64 }
    - { outdoor_c: 5,  slider_c: 68 }
    - { outdoor_c: 0,  slider_c: 70 }
    - { outdoor_c: -3, slider_c: 75 }
  min_change_c: 2
  override_grace_minutes: 720
```

The curve values are **slider positions**, not flow temperatures: Tado scales the
slider down by its own weather adjustment (about 0.8 × slider + 3 at 10 °C), so
slider 64 ≈ 54 °C effective in mild weather. Moving the slider in the Tado app is
respected for `override_grace_minutes`, then the curve takes over again. To tune,
run `python scripts/flow_report.py --region <region> --days 14` and adjust the
`slider_c` nearest the outdoor band in question: raise it where several rooms
struggle, lower it where no room needs more than ~70% valve. If Tado's
auto-adaptation is ever switched off, the reconciler stops touching the slider.

**Re-authenticate** — if the refresh token is ever revoked, re-run
`scripts/bootstrap_auth.py`.

**Logs** — CloudWatch Logs group `/aws/lambda/tado-dhw-scheduler`. Each run logs
whether it was a no-op or applied a change.

**Run cadence / timezone** — override the `ScheduleExpression` and
`ScheduleTimezone` stack parameters in `template.yaml` (defaults: `rate(5 minutes)`,
`Europe/London`).

## Migrating from the old EC2 architecture

This project previously ran a 24/7 container on an EC2 `t4g.nano` instance in a
CloudFormation stack named `tado-dhw-scheduler`. That stack owns the
`tado-github-deploy-role` IAM role, the GitHub OIDC provider, **and the same
stack name the new app uses** — so you must **delete it before deploying the new
stack**, not after. Deleting it is also what stops the EC2 charges. Full delete-first
sequence and caveats (ECR gotcha, downtime) are in
[`docs/FIRST_DEPLOY.md`](docs/FIRST_DEPLOY.md#migrating-from-the-old-ec2-stack-delete-it-first).
