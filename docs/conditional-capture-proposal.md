# Connection-bound capture reads

This extends the conditional album interface; it is not an activation procedure.
Cleanup consumers must remain disabled until the extension is independently
reviewed, qualified, released and adopted with their separately reviewed
integration. The same disabled-by-default custody requirements apply.

## Problem

An album listing's `connection_epoch` does not bind later `photos_get` or
`photos_download` calls. Those calls may retain older session or thumbnail
state after a same-number snapshot replacement. Bracketing them with bound
album listings cannot establish which connection supplied metadata or bytes.

## Interface

- Add the existing optional 64-hex `expected_connection` argument to
  `photos_get` and `photos_download`; return that same `connection_epoch` on
  successful conditional responses. Keep existing unbound behavior compatible.
- Use the existing account admission and accepted-publication transaction for
  the whole read or download-copy creation. Reject unsupported, missing-ready,
  changed or pending bindings before provider work, and reject a publication
  that becomes pending before returning. Do not substitute a new token.
- Use a fresh conditional session, sender, globals and metadata caches while
  sharing existing identity policy, request budgets, pacers and failure policy.
  Conditional image reads must neither use nor populate the shared thumbnail
  cache keyed only by media ID and size. No cache framework or new worker.
- Create download links from immutable JPEG bytes obtained under that binding.
  Retain the existing one-use, expiry, account admission, revocation and output
  response barriers. A later link fetch serves the retained copy; it must not
  fetch replacement-provider bytes. It is not an observation of current album
  membership. Do not weaken existing link-access checks.
- Per-item errors remain errors. A bound outer response does not prove that a
  requested item's details or image were captured. Add no mutation, retry,
  credential, grant, account-routing or provider allowlist authority.
- Before conditional success, validate the provider media identity in every
  extended/basic detail record used: reject missing, invalid, mismatched or
  conflicting IDs. A partial metadata response must still establish the requested
  identity; disclose unavailable optional fields. Apply the same validation to
  album-list `details=true` enrichment, download resolution and thumbnail
  re-resolution before mixing metadata or retaining any usable link. A rejected
  conditional enrichment fails with an error, without a usable binding/coverage.

## Required consumer behavior

Capture retains its original provider scope, album/media IDs and connection
binding. Every capture page, detail read and JPEG-copy creation must use and
return that binding; validate each requested item and download result. Native
snapshot numbers are freshness diagnostics, never connection identity. Retain
reduced-JPEG and metadata limitations in preview/consent.

Legacy captures and cleanup intents without a verified original binding remain
attention-only; never obtain a new token to retrofit their provenance. A group
must retain and verify each constituent binding before any future cleanup. All
chosen Wiki jobs must have accepted revisions and explicit removal consent.
Unknown writes remain observation-only, never replayed. This interface does not
enable cleanup or extend the current runtime permission boundary.

## Required synthetic counterexamples

Exercise the MCP boundary with actual scripted transport: warm old session and
thumbnail state, publish replacement credentials with the same numeric snapshot
generation, and compare metadata/JPEG results in warmed and fresh processes.
Cover reconnect/publication between listing and each read, missing/unready and
unsupported bindings, pending publication before/after work, request budgets,
per-item errors, immutable link bytes and existing expiry/revocation barriers.
Cover missing/wrong/invalid IDs, conflicting extended/basic identities and
malformed detail records; rejected download items must have no usable link.
Prove assertions fail on the prior interface or cache path before fixes. No real
provider, credential, account, model or host calls.
