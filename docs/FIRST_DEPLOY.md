# First Deploy Runbook

A step-by-step, one-time guide to standing up the serverless Tado DHW Scheduler
(or cutting over from the old EC2 version). Once this is done, ongoing changes
are just "push to `main`" (CI) or "edit the SSM config parameter" (schedule).

For the day-to-day and the architecture overview, see the main [`README.md`](../README.md).

---

## ⚠️ Use a personal AWS account — never a work/corporate one

Everything here provisions personal infrastructure. Do **not** deploy it into a
corporate/work AWS account: it's a misuse of that account and is usually blocked
by permission boundaries anyway.

**The guard — run this before every AWS command and read the output:**

```bash
aws sts get-caller-identity
```

Confirm the `Account` / `Arn` is **your personal account**. If it's a work
account (e.g. the ARN mentions a corporate user, an SSO/asset identity, or an
account number you recognise as work), **stop** and fix your profile before
continuing.

---

## 0. Prerequisites

- A **personal** AWS account.
- Tools installed locally: **AWS CLI v2**, **Python 3.11+**, **git**, and the
  **GitHub CLI** (`gh`) or access to the repo settings in a browser.
- A web browser (for the AWS console, and to approve Tado access).
- This repo cloned locally.

> `sam` is **not** required locally — CI runs the SAM build/deploy. The only AWS
> work you do by hand is steps 1, 3, and 6.

---

## Migrating from the old EC2 stack? Delete it first

**Skip this if you're deploying fresh** (no prior EC2 version).

The original EC2 deployment lives in a CloudFormation stack named
`tado-dhw-scheduler` — **the same name the new app stack uses** — and it owns the
`tado-github-deploy-role` IAM role and the GitHub OIDC provider. The new setup
reuses all three names, so you must delete the old stack **before step 3**, not
after. Tell-tale sign you skipped this: step 3 fails with
`tado-github-deploy-role already exists in stack .../tado-dhw-scheduler/...`.

Do this as an **admin/root** identity in your personal account — the scoped
bootstrap user from step 1 can't delete the old EC2/ECR resources.

1. **CloudFormation → Stacks → `tado-dhw-scheduler` → Delete**, and wait for
   `DELETE_COMPLETE`.
   - **ECR gotcha:** if the stack's ECR repo still contains images, the delete
     fails on that resource. Empty the repo (delete the images) and retry, or
     delete the repo manually, then retry the stack delete.
2. If an earlier failed attempt left a `tado-dhw-deploy-role` stack in
   `ROLLBACK_COMPLETE`, delete that too — CloudFormation won't re-create over a
   rolled-back stack.

Implications, all expected:

- Your **current hot-water automation stops** until the new stack is live and
  token-seeded (steps 3–6). Fine for a short cutover.
- The old EC2 instance and its stored OAuth token are destroyed — you re-seed the
  token in step 6.
- Because the old OIDC provider and role are now gone, in step 3 you **leave
  `ExistingOidcProviderArn` blank** and let the template create fresh ones.

---

## 1. Create the bootstrap operator identity (once)

CI authenticates with short-lived OIDC credentials and needs no stored keys. But
the manual steps below (deploying the deploy role in step 3, seeding the token in
step 6) need local AWS credentials. Instead of root keys or a broad admin user,
use a dedicated identity scoped by [`iac/bootstrap-user-policy.json`](../iac/bootstrap-user-policy.json).

That policy grants **only**: deploy/update/delete the `tado-dhw-deploy-role`
CloudFormation stack; manage only the `tado-github-deploy-role` IAM role and the
GitHub OIDC provider; and `PutParameter` on `/tado/token` (plus the SSM-scoped
KMS encrypt a SecureString needs). Nothing else.

### Option A — IAM Identity Center (SSO), preferred

No long-lived keys on your machine:

```bash
aws configure sso --profile tado-personal
aws sso login --profile tado-personal
```

Attach the contents of `iac/bootstrap-user-policy.json` (with placeholders
substituted, see below) to the permission set you sign in as.

### Option B — a dedicated IAM user

In the AWS console (signed into your personal account), **IAM → Users → Create user**:

1. Name it e.g. `tado-bootstrap`. Leave **console access unchecked** — this is a
   CLI-only identity. On the permissions screen, attach nothing yet → **Create user**.
2. Open the user → **Permissions → Add permissions → Create inline policy → JSON**.
   Paste `iac/bootstrap-user-policy.json`, first replacing `<ACCOUNT_ID>` and
   `<REGION>`. Name it `tado-bootstrap` → **Create policy**.

   Or do the substitution + attach from the CLI (as an existing admin):
   ```bash
   ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
   REGION=eu-west-2
   sed "s/<ACCOUNT_ID>/$ACCOUNT_ID/g; s/<REGION>/$REGION/g" \
     iac/bootstrap-user-policy.json > /tmp/tado-bootstrap-policy.json
   aws iam put-user-policy --user-name tado-bootstrap \
     --policy-name tado-bootstrap --policy-document file:///tmp/tado-bootstrap-policy.json
   ```
3. **(Recommended) Enable MFA** on the user (*Security credentials → Assign MFA
   device*). Note: MFA protects console sign-in; it does **not** gate CLI
   access-key use — the real protection for the keys is the create → use → delete
   hygiene in step 7.
4. **Create an access key**: *Security credentials → Create access key → Command
   Line Interface (CLI)* → copy the **Access key ID** and **Secret** (the secret
   is shown only once).
5. Store it in a named profile:
   ```bash
   aws configure --profile tado-personal
   # AWS Access Key ID:     <paste from step 4>
   # AWS Secret Access Key: <paste from step 4>
   # Default region name:   eu-west-2
   # Default output format: json
   ```

> **Gotcha — "The config profile (tado-personal) could not be found".**
> This means the profile was never written. Causes: the `aws configure` prompt
> was cancelled/closed early; or you set `export AWS_PROFILE=tado-personal`
> *before* the profile existed, so every later command errors. Fix: run
> `aws configure --profile tado-personal` (Option B) or `aws configure sso
> --profile tado-personal` (Option A) to completion, then confirm with
> `aws configure list-profiles | grep tado-personal`.

---

## 2. Point your shell at the profile and verify

```bash
export AWS_PROFILE=tado-personal
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN   # clear stray creds
aws configure list-profiles | grep tado-personal                 # profile exists?
aws sts get-caller-identity                                      # personal account?
```

✅ `Account` is your personal account and the ARN is your `tado-bootstrap` user
(or SSO identity). ❌ Anything work-related → stop and fix the profile.

> `export AWS_PROFILE` lasts only for the current terminal session. Re-run the
> verify command whenever you open a new shell.

---

## 3. Deploy the GitHub Actions deploy role

```bash
cd <repo root>
REGION=eu-west-2   # match your configured region

aws cloudformation deploy \
  --template-file iac/deploy-role.yaml \
  --stack-name tado-dhw-deploy-role \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$REGION" \
  --parameter-overrides GitHubOrg=<your-gh-username> GitHubRepo=tado-dhw-scheduler
```

**Collision on `tado-github-deploy-role` or the OIDC provider?** That means the
old EC2 stack is still up — it owns both (and the app stack name too). Go do
[Migrating from the old EC2 stack](#migrating-from-the-old-ec2-stack-delete-it-first)
above, then retry this step with `ExistingOidcProviderArn` left blank.

If instead an OIDC provider exists for some *other* reason you don't want to
delete, reuse it rather than creating a duplicate:

```bash
  ExistingOidcProviderArn=arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com
```

Find that ARN under **IAM → Access management → Identity providers** (global —
region doesn't matter), or via `aws iam list-open-id-connect-providers`.

Then read the role ARN from the outputs (you'll need it in step 4):

```bash
aws cloudformation describe-stacks --stack-name tado-dhw-deploy-role --region "$REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='DeployRoleArn'].OutputValue" --output text
```

---

## 4. Configure the GitHub `production` environment variables

The CI workflow reads these. Set via `gh`:

```bash
gh variable set AWS_DEPLOY_ROLE_ARN --env production --body "<arn-from-step-3>"
gh variable set AWS_REGION          --env production --body "$REGION"
```

…or in the browser: repo **Settings → Environments → `production` → Variables**.

| Variable | Value |
|----------|-------|
| `AWS_DEPLOY_ROLE_ARN` | the role ARN from step 3 |
| `AWS_REGION` | your region, e.g. `eu-west-2` |

---

## 5. Deploy the application stack

Push to `main`, merge the migration PR, or run the **Deploy Tado DHW Scheduler**
workflow manually (`workflow_dispatch`). CI (OIDC → `sam build` → `sam deploy`)
creates the Lambda, the 5-minute EventBridge schedule, and the `/tado/config`
parameter.

Wait for the workflow to go green before the next step — the token you seed needs
a Lambda to consume it.

---

## 6. Seed the OAuth token

The Tado OAuth Device Flow needs a human to approve access in a browser, so it
can't run in Lambda. Run it locally (still on `AWS_PROFILE=tado-personal`); it
writes the token into the `/tado/token` SecureString the Lambda reads:

```bash
pip install boto3 requests        # use a venv if you prefer
python scripts/bootstrap_auth.py --region "$REGION"
```

1. It prints a **Tado authorization URL** and a code.
2. Open the URL, confirm the code matches, approve access.
3. It polls, then prints `Token written to SSM SecureString parameter: /tado/token`.

Every scheduled run afterwards is autonomous via the stored refresh token.

---

## 7. Verify

```bash
aws logs tail /aws/lambda/tado-dhw-scheduler --follow --region "$REGION"
```

A healthy run logs either `no-op` (already at the target setpoint) or `applied`
(pushed a change). If it logs "No Tado token found … run bootstrap_auth.py", go
back to step 6.

---

## 8. Clean up

Once the Lambda is confirmed working:

- **Delete the bootstrap access key** — IAM → `tado-bootstrap` → Security
  credentials → deactivate & delete the key. Keep the *user* (with no active
  keys) so re-runs are easy. CI needs no keys.
- **Old EC2 stack** — if you're migrating, you already deleted it up front (see
  [Migrating from the old EC2 stack](#migrating-from-the-old-ec2-stack-delete-it-first)),
  which is what stops the EC2 charges. Nothing left to tear down here.

---

## Re-running later

You rarely need this — CI handles ongoing deploys. But to run a manual step again
(e.g. re-authenticate after a revoked token, or redeploy the deploy role):

1. `export AWS_PROFILE=tado-personal` and verify the account (step 2).
2. If you deleted the access key, create a fresh one (IAM → `tado-bootstrap` →
   Create access key) and re-run `aws configure --profile tado-personal`.
3. Run the step you need (`scripts/bootstrap_auth.py`, or the `cloudformation
   deploy` from step 3).
4. Delete the access key again (step 8).

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `The config profile (tado-personal) could not be found` | Profile was never written. Re-run `aws configure [sso] --profile tado-personal` to completion; don't `export AWS_PROFILE` before it exists. See the gotcha in step 1. |
| `AccessDenied` on `iam:*` / `cloudformation:*` | You're on the wrong (corporate) identity, or the bootstrap policy placeholders weren't substituted. Check `aws sts get-caller-identity` and the inline policy JSON. |
| step 3 fails: `tado-github-deploy-role already exists in stack .../tado-dhw-scheduler/...` | The old EC2 stack still exists and owns that role, the OIDC provider, and the app stack name. Delete it first — see [Migrating from the old EC2 stack](#migrating-from-the-old-ec2-stack-delete-it-first). |
| `cloudformation deploy` fails: OIDC provider already exists (not from the old stack) | Reuse it: pass `ExistingOidcProviderArn=...` (step 3). |
| CI workflow fails at "Configure AWS credentials" | `AWS_DEPLOY_ROLE_ARN` / `AWS_REGION` not set in the `production` environment, or the deploy role's trust policy doesn't match `repo:<org>/<repo>`. |
| Lambda logs "No Tado token found" | Token not seeded yet — run step 6. |
