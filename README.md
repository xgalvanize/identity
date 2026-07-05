# Global Identity Namespace (FastAPI + GraphQL + MongoDB)

This repo deploys a shared identity service to Kubernetes in its own namespace so multiple apps can plug into one user system.

## Why This Design

- Uses your preferred stack: FastAPI + GraphQL + MongoDB.
- Keeps identity in namespace `global-identity`.
- Lets each application keep its own separate database for domain data.
- Avoids mandatory Postgres.

## Repository Layout

- `app/main.py`: identity API service
- `k8s/`: Kubernetes manifests
- `docs/integration.md`: app integration guide

## What Gets Deployed

- Namespace: `global-identity`
- MongoDB StatefulSet + Service
- Identity API Deployment + Service + HPA
- Ingress host: `identity.lan`
- Network policies (default deny + required allows)

## Build And Deploy (Local-Only Image Flow)

The deployment references image:
- `ghcr.io/xgalvanize/global-identity:latest`

If you want to stay local-only and avoid remote registries, build image on `thunderball` directly and retag to the same name.

### 1) One-command deploy (recommended)

From this workspace:

```bash
./scripts/deploy.sh
```

This script will:
- generate fresh Mongo and JWT secrets
- sync this repo to `thunderball`
- build container image on `thunderball` using first available path:
	- `nerdctl --namespace k8s.io`
	- `docker build` + `sudo k3s ctr images import -`
	- `podman build` + `sudo k3s ctr images import`
	- if no remote builder exists: local build on `speedball` and stream image to `thunderball` via `sudo k3s ctr images import -`
- apply all manifests from `speedball` with your kubeconfig
- wait for rollout and print status

### 2) Manual flow if you prefer

#### Copy repo to thunderball

```bash
rsync -av --delete /home/borg/Desktop/identity/ borg@thunderball:/home/borg/identity/
```

#### Build image on thunderball

```bash
ssh borg@thunderball 'cd /home/borg/identity && sudo nerdctl --namespace k8s.io build -t ghcr.io/xgalvanize/global-identity:latest .'
```

If `nerdctl` is not installed, the deploy script automatically falls back to docker/podman import paths when available.

#### Set secrets

```bash
cp k8s/secrets.example.yaml k8s/secrets.yaml
```

Edit `k8s/secrets.yaml` values before apply.

#### Apply manifests from this laptop (speedball)

```bash
KUBECONFIG=/home/borg/.kube/k3s-remote kubectl apply -f k8s/namespace.yaml
KUBECONFIG=/home/borg/.kube/k3s-remote kubectl apply -f k8s/secrets.yaml
KUBECONFIG=/home/borg/.kube/k3s-remote kubectl apply -f k8s/mongodb.yaml
KUBECONFIG=/home/borg/.kube/k3s-remote kubectl apply -f k8s/identity-api.yaml
KUBECONFIG=/home/borg/.kube/k3s-remote kubectl apply -f k8s/ingress.yaml
KUBECONFIG=/home/borg/.kube/k3s-remote kubectl apply -f k8s/network-policy.yaml
```

#### Verify rollout

```bash
KUBECONFIG=/home/borg/.kube/k3s-remote kubectl -n global-identity get pods,svc,ingress
KUBECONFIG=/home/borg/.kube/k3s-remote kubectl -n global-identity rollout status deploy/identity-api
./scripts/verify.sh
```

## Plug-In Model For Apps

Each app integrates by:

1. Calling `POST /auth/login` to get tokens.
2. Sending `access_token` as bearer token to app backend.
3. App backend calling `POST /auth/introspect` to validate token.
4. Storing `sub` as stable user key in app-local DB.

See full instructions in `docs/integration.md`.

## Firebase Integration (Optional)

Short answer: yes, and this repo now defaults to Firebase-first auth to keep the setup simple.

Recommended model in this repo:
- clients authenticate with Firebase Auth and obtain a Firebase ID token
- clients call `POST /auth/firebase/exchange` on this service
- this service verifies Firebase token server-side and returns local access/refresh tokens
- all internal apps continue validating only this service's tokens

Why this is safer:
- keeps one consistent token format and issuer in your platform
- avoids every app needing Firebase Admin SDK and Firebase policy logic
- supports gradual migration between providers later

Default behavior in this repo:
- Firebase exchange is the primary login path.
- Legacy local `/auth/register` and `/auth/login` are disabled unless explicitly enabled.

Enable in Kubernetes:
1. set these keys in `k8s/secrets.yaml` under `identity-secret`:
	- `enable-firebase-auth: "true"`
	- `allow-local-password-auth: "false"` (recommended)
	- `firebase-project-id: <your-project-id>`
	- `firebase-service-account-json: |` with full service-account JSON
2. apply updated secrets and deployment:

```bash
KUBECONFIG=/home/borg/.kube/k3s-remote kubectl apply -f k8s/secrets.yaml
KUBECONFIG=/home/borg/.kube/k3s-remote kubectl apply -f k8s/identity-api.yaml
KUBECONFIG=/home/borg/.kube/k3s-remote kubectl -n global-identity rollout status deploy/identity-api
```

If you need the old local email/password endpoints temporarily, set:
- `allow-local-password-auth: "true"`

## Important Production Notes

Current baseline is good for internal LAN and early platform consolidation.
Before internet exposure, implement:
- RS256 keypair + real JWKS
- login rate limiting and lockout
- MFA and password reset flows
- centralized audit logs
- backup and restore runbooks for MongoDB

## Service Ports

- Identity API container: `8080`
- Identity API service: `80`
- MongoDB: `27017`
