# Plugging Apps Into Global Identity

This guide shows how any app in your cluster can use the shared identity service in the `global-identity` namespace.

## Endpoints

Public ingress host:
- `https://identity.lan`

Core APIs:
- `POST /auth/register` (legacy optional)
- `POST /auth/login` (legacy optional)
- `POST /auth/refresh`
- `POST /auth/firebase/exchange` (recommended)
- `POST /auth/introspect`
- `GET /auth/me`
- `PATCH /auth/me`
- `POST /graphql`

Discovery endpoints:
- `GET /.well-known/openid-configuration`
- `GET /.well-known/jwks.json` (placeholder for future RS256 migration)

## Recommended Auth Flow For Web Apps

1. Client signs in with Firebase Auth (email/password reset and phone verification live in Firebase).
2. Client sends Firebase ID token to `POST https://identity.lan/auth/firebase/exchange`.
3. Identity returns:
   - `access_token` (short-lived)
   - `refresh_token` (longer-lived)
4. App stores refresh token in an HttpOnly secure cookie.
5. App sends access token in `Authorization: Bearer <token>` on API calls.
6. App backend validates token by calling `POST /auth/introspect`.
7. App maps `sub` (subject) to local user record in its own DB.

Legacy local auth endpoints can be re-enabled only when needed by setting `allow-local-password-auth` to `"true"` in the identity secret.

## Example Login Request

```bash
curl -sS https://identity.lan/auth/login \
  -H 'content-type: application/json' \
  -d '{"email":"alice@example.com","password":"StrongPassword123"}'
```

## Example Introspection Request

```bash
curl -sS https://identity.lan/auth/introspect \
  -H "Authorization: Bearer $ACCESS_TOKEN"
```

## Example Firebase Exchange Request

Use this when the client already signed in with Firebase Auth and has an ID token:

```bash
curl -sS https://identity.lan/auth/firebase/exchange \
  -H 'content-type: application/json' \
  -d '{"id_token":"<firebase-id-token>"}'
```

The response is this service's normal token pair (`access_token`, `refresh_token`).

## App-Side Data Model Guidance

In each app database, keep:
- `identity_subject` (string, required, indexed, unique per app user)
- `email_cache` (optional)
- app-specific profile and domain data

Do not store:
- password hashes
- refresh token source-of-truth

## Optional GraphQL Profile Query

```graphql
query {
  me {
    subject
    email
    displayName
    createdAt
  }
}
```

Send bearer token in header:
- `Authorization: Bearer <access_token>`

## Namespace-to-Namespace Access

If app runs in namespace `aichatbot`, call identity by cluster DNS:
- `http://identity-api.global-identity.svc.cluster.local`

For internal-only calls, you can skip ingress and call service DNS directly.

## Security Hardening Checklist

- Switch JWT from `HS256` to `RS256` and publish real JWKS keys.
- Add rate limiting for login and register endpoints.
- Add lockout and bot protection.
- Add email verification and password reset flows.
- Encrypt MongoDB disk and backups.
- Rotate `jwt-secret` and database credentials.
- Restrict ingress source IPs if possible.

## Multi-App Authorization Pattern

Use shared identity for authentication and app-local authorization:
- Authentication: who the user is (`sub` from global identity)
- Authorization: what they can do (roles/permissions in each app DB)

This keeps identity global and business permissions local to each app.
