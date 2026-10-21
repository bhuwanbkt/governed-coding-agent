# ADR 0012: Server-rendered web UI with OIDC sign-in (Zitadel)

- **Status:** Accepted
- **Date:** 2026-10-20

## Context

Phases 1–4 were usable only through the JSON API with static API keys (`API_KEYS`). Reviewers
need to read a plan, a diff and the governance findings and then approve or reject, and people
should sign in with an identity provider rather than share keys. Roles (who may submit, who may
approve) belong in that identity provider, not in an environment variable.

## Decision

- **UI:** server-rendered Jinja2 pages in the same FastAPI app (`/ui`). There's no SPA build step
  and no third-party JavaScript: a small `static/app.js` refreshes the job timeline while a job
  runs, and stops when the server sends `X-Poll-Stop`.
- **Sign-in:** OpenID Connect Authorization Code flow with PKCE (S256), `state` and `nonce`, and
  a confidential client (client secret). The ID token is verified against the provider's JWKS:
  signature, issuer, audience, expiry and nonce. Zitadel is the provider; any OIDC provider works
  with `OIDC_ROLES_CLAIM`.
- **Roles:** `submitter` and `approver` are Zitadel project roles, read from the ID token
  (`urn:zitadel:iam:org:project:{id}:roles`), with userinfo as a fallback.
- **Session:** a signed cookie (`gca_session`) with the username, roles, expiry and a CSRF token.
  It's HttpOnly, SameSite=Lax and Secure in production, and expires after `SESSION_MAX_AGE_HOURS`.
  Provider tokens are not kept.
- **Same rules as the API:** the UI calls the same data layer. Separation of duties, the diff
  SHA-256 binding and approval expiry are enforced there and by the database trigger (ADR 0004,
  0006). Every form has a CSRF token. Pages send a strict CSP with no inline scripts, plus
  `frame-ancestors 'none'`, `nosniff` and `no-store`. Agent output and repository content are
  always escaped.
- **The JSON API stays API-key only.** It ignores the session cookie, so a browser session can't
  be used for cross-site requests against it.

## Consequences

- One deployable and no frontend toolchain. The UI is simple, but it's enough for review work.
- Names are the user's `preferred_username`. A UI user and an API-key user with the same name
  count as the same person for separation of duties, which is the safe direction.
- `APPROVERS` applies to API-key users. UI users need the `approver` role.
- Roles are read at sign-in. A removed role takes effect at the next sign-in, at most
  `SESSION_MAX_AGE_HOURS` later. Revoking access sooner means a shorter session or a server-side
  session store.
- Without `SESSION_SECRET` (development only), sessions end when the process restarts. Production
  refuses to start with OIDC configured and no secret.
