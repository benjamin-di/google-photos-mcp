# Conditional album cleanup

Automatic cleanup must retain the original album, exact item IDs and original
connection binding. `freshness.generation` is a session snapshot number, **not**
connection identity. Equal numbers from different connections prove nothing.

## Supported custody

Conditional operations are disabled by default. Set
`PHOTOS_CONDITIONAL_NATIVE_ACCOUNT` to the configured default account ID only
after verifying that **every** publisher of that account's snapshot uses
`PublicationGuard` against the same durable connection control database as the
reader. The native human publisher and guarded native maintenance path support
this serialization only when upgraded to the accepted-publication contract
described below. The account must already have an adopted owner and a
noninitial connection epoch with a completed publication receipt;
`PHOTOS_DEFAULT_LOGIN_URL` must identify its native
connection service. These settings do not enroll an owner, establish grants,
configure a publisher, or verify an installed deployment by themselves.

Further-account and standalone snapshot exporters do not provide this custody
contract. They cannot use conditional calls or receive usable connection tokens.
Do not opt in an account with an unguarded publisher. Out-of-band file replacement
or control database restoration invalidates the contract; stop automation and
re-establish the original binding explicitly rather than treating restored or
replacement state as proof of the original capture.

## Interface

1. Start `photos_list(source="album", album_id=...)` without a cursor. A
   supported managed listing returns an opaque `connection_epoch` token.
2. Retain that token with the capture. Pass it as `expected_connection` to
   subsequent album listings, `photos_get`, `photos_download` and
   `photos_album_remove`. Detail and JPEG capture must follow the
   [bound capture contract](conditional-capture-proposal.md). It is a precondition,
   not a credential or grant. Missing tokens must never be replaced with a new
   listing's token to repair an old capture automatically.
3. Conditional calls compare the original token and hold the native publication
   transaction through provider work, including mutation and read-back. Epoch
   rotation, guarded credential publication and conditional operations serialize
   in the same SQLite database. Pending publication refuses both before and after
   the call. Control contention can return `busy`; the connection UI must not
   report disconnect success while it remains unconfirmed.
4. A mismatch or unsupported conditional call returns `connection_changed`
   without provider work. Unbound reads and human-requested removals retain their
   existing behavior, without an original-connection guarantee.

The existing control database gains an additive `accepted_publications` table
when the upgraded guard first publishes. Legacy controls alone never authorize
conditional capture. The owner reconnect path enables a new epoch before new
credentials are accepted: rotation alone cannot authorize conditional capture,
even though the older snapshot is still readable by ordinary tools.
A successful guarded publication records a new accepted binding for that epoch.
Readers hold that exact receipt under the publication transaction. Every snapshot
publication, including an ordinary refresh under the same epoch, invalidates
earlier tokens. The shared guard cannot distinguish a refresh from a replacement;
it must not preserve a token based on that assumption. Captures that outlive a
refresh therefore require attention, rather than automatic rebinding. A capture
after an intermediate maintenance publication during reconnect likewise cannot
survive the later human replacement, even when both use the same epoch and native
snapshot number.

Before any external snapshot writes, publication durably records an unready
receipt, then reacquires and holds the database transaction while publishing.
Conditional reads refuse during the gap. Only successful completion makes the
receipt ready. An exception or process interruption leaves it unready despite
transaction rollback; reopening the database cannot restore the old binding.
A later successful guarded publication establishes a different binding. Do not
clear that hold or reuse an old capture token automatically.

Only a complete verified snapshot publisher may use `PublicationGuard.snapshot`.
Status-only writes use `PublicationGuard.status`: they fence access but never
create, replace or clear an accepted snapshot receipt. The legacy generic
`publish` context invalidates acceptance and cannot establish a ready receipt.
Writing `keeper.json` after a failed cookies/metadata publication must leave that
publication unready. Native snapshot call sites must explicitly select snapshot
acceptance, and owner-status call sites must explicitly select status fencing.

Rehearse the additive schema on copied synthetic controls. Before opting in,
qualify and adopt the upgraded guard in **all** native publishers as well as the
reader, and obtain a receipt from the next necessary normal publication. Existing
guarded publisher versions do not create this receipt; installing the reader or
setting an environment variable alone cannot establish the contract. Disable
conditional automation before rolling a publisher back to an older guard.

Bound operations use fresh, isolated listing pages and album metadata. Concurrent
unbound calls cannot populate those caches with data from an earlier connection.
Each conditional operation also uses a fresh session reader and HTTP context,
with shared request budgets and pacing. Neither session nor HTTP state may be
reused solely because native snapshot generation numbers are equal or increasing;
an accepted replacement with the same number must use its actual credentials in
both warmed and newly started processes.
Conditional album pages reject malformed item containers or records, invalid item
IDs, malformed continuation tokens, and missing or mismatched album identity with
`schema_changed`. Only an explicit empty item list with matching album metadata
and a terminal continuation (`null` or an empty string) can describe an empty
page. Errors never carry usable binding or coverage evidence.
A successful page is not a complete listing: follow pagination under the same
binding, obey coverage and freshness, and reject recovered cursors, loops,
incomplete coverage or any error as absence evidence. Album count deltas, including
`removed`, never prove that particular IDs are absent. Concurrent library edits
remain possible; this protocol fences connection replacement, not Google's library.

## Mutation evidence

Errors remain nested as `{"error":{"code":...,"next_step":...,"retryable":...}}`.
Removal errors additionally carry invocation-level `mutation`:

| `phase` | `definitely_unsent` | Meaning |
| --- | --- | --- |
| `before_write` | `true` | This invocation did not hand a mutation to the sender. |
| `write_attempted` | `false` | A mutation may have reached the provider. |
| `read_back` | `false` | A mutation returned, but later observation failed. |

The transport marks the handoff immediately before calling the mutation sender,
after local request budget and pacing checks. A code such as `busy`,
`budget_exhausted`, `refused` or `schema_changed` alone proves nothing about phase.
Unknown or malformed envelopes do not prove unsent. Successful removal also
returns `mutation` with `read_back` and `definitely_unsent:false`; it is not an
exact-ID absence receipt.

The transport's evidence describes one RPC, not an entire multi-write tool.
Only removal tracks its complete invocation's precise phases. Other changing
tools expose such evidence conservatively as phase `write_attempted` with
`definitely_unsent:false`;
creating an album and then failing to send its add-items request must never imply
that the whole creation invocation was unsent.

Removal errors after attempted writes are nonretryable and explicitly forbid automatic
repetition. Network/5xx/parse failures are never replayed; the existing single
refresh after a definite HTTP 400/403 rejection remains bounded. A pre-send
refusal proves only this invocation was unsent, never that an earlier durable
intent was unsent. Lost replies and process interruption therefore require
original-binding observation or attention, not another removal.

Source implementation and synthetic checks do not establish deployment custody,
provider release/adoption, or a consuming application's cleanup acceptance.
