# Pairing protocol v1

## QR payload

The QR body is UTF-8 JSON with `v`, `url`, `pair`, and `id` fields. Version is the integer `1`. `url` is an absolute HTTP or HTTPS gateway URL without user information, query, or fragment. `pair` has the form `<8 lowercase hex>.<32 lowercase hex>`. `id` is the member identifier. The QR does not contain the long-lived client token.

## HTTP endpoints

`POST /pair/claim` is unauthenticated. Its source gate, `_pairing_claim_source_allowed`, accepts loopback, tailnet IPv4 in `100.64.0.0/10`, and the Tailscale IPv6 ULA `fd7a:115c:a1e0::/48`. The QR-delivery peer gate intentionally remains narrower: `_tailnet_or_loopback_peer` accepts loopback and tailnet IPv4 only, not the Tailscale IPv6 ULA. `CATY_PAIRING_ALLOW_NONTAILNET=1` is an unsupported advanced override that opens `/pair/claim` to every peer able to reach the gateway; it does not widen the QR-delivery peer gate. The claim JSON body contains `pair` and may contain a display-only `device` object. A successful response returns `ok`, `v`, `url`, `token`, and `id`. Malformed input returns 400; an invalid or revoked credential returns 401; a consumed credential returns 409; an expired credential returns 410; an oversized body returns 413; rate limiting or lockout returns 429; and disabled pairing or an unavailable store returns 503.

`POST /pair/new` requires existing write authentication. It revokes any live credential for the member and returns `ok`, `v`, `url`, `pair`, `id`, and `expires_at`. `POST /pair/revoke` also requires write authentication and immediately removes the live credential. Pairing secrets must not be written to logs or standard output.

## Store behavior

The store is disk-authoritative. Its root is mode 0700 and records are mode 0600. A record stores only a SHA-256 digest of the secret and includes member, URL, creation, expiry, failure, and lockout fields. Writes use a same-directory temporary file, fsync, atomic replace, and directory fsync. A persistent advisory lock serializes processes.

One live credential is allowed per member. Creating a new one commits the new record before removing the old one. Claim is at-most-once: a successful claimant removes the record and writes a consumed tombstone; expiry writes an expired tombstone. Tombstones remain through the credential TTL plus 24 hours. Five consecutive failures trigger a 60-second lockout, and 50 total failures revoke the credential. Default credential TTL is 600 seconds. Claim rate limiting uses a fixed 60-second window.
