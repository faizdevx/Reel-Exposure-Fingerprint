[HIGH] No supported hosted deployment: no TLS, insecure cookie default, plaintext Bearer token

**Fixed on 2026-10-09** with `docker-compose.hosted.yml`, `Caddyfile`, the production cookie guard and an HTTPS-only extension build (H-4). Not run against a real domain. Tests: `tests/test_hosted_config.py`, `extension/test/api-base.test.ts`.

**Severity:** HIGH for any use beyond localhost (not a vulnerability of the local setup). **Area:** transport security, session handling, secret handling. **Source:** `docs/security-audit.md`, H-4.

## Problem

Spec N6 and `docs/privacy.md` say "TLS whenever the server is not on localhost". There is no way to satisfy that today:

- `docker-compose.yml` serves HTTP only; ADR-019's hosted mode (Caddy, TLS, nightly dump) is M7 and not built.
- `dashboard/nginx.conf` listens on port 80 and forwards only `Host` (no `X-Forwarded-*`).
- The session cookie is `Secure` only when `REF_ENVIRONMENT=production` (`server/app/config.py`), and compose defaults that variable to `local`. Probe on 2026-10-09: the default `secure_cookies` is `False`.
- The extension's server address is hard-coded: `API_BASE = 'http://127.0.0.1:8000'` (`extension/src/config.ts`), with host permissions for localhost only. It cannot be pointed at an HTTPS server.
- Nothing refuses to start in a non-local setting without TLS, and nothing warns.

## Impact

In local mode (ports bound to `127.0.0.1`) this is acceptable. If anyone exposes the stack through a domain or tunnel for a cohort, the session cookie and the extension's Bearer token cross the network in clear text, and the cookie lacks `Secure` unless the operator knows to change two settings.

## Suggested fix (not applied)

- Build the hosted mode: a TLS-terminating proxy with HSTS, forwarded headers handled, the API reachable only on the internal network.
- Let the extension take a configured HTTPS base URL through an optional-permission request.
- Make `Secure` the default for the cookie (opt out only for `local`) and make the API refuse `REF_ENVIRONMENT=production` without secure cookies.
- Until then, state in the README that the stack is local-only, and add the check to CI.

## Acceptance

- A documented hosted deployment passes a TLS check, and the cookie carries `Secure`, `HttpOnly` and `SameSite=Lax`.
- The API refuses to start in `production` with insecure cookies.
- The extension talks to a configured HTTPS server and refuses plain HTTP for a non-local host.
