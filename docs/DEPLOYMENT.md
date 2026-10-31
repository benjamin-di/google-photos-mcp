# Cooperative deployment holds

The HTTP reader, further-account login service and default-account human service
require pre-provisioned deployment control records before starting this source.
This is a source protocol, not evidence that an older or installed image supports
it. There is no application initialization, migration or repair command here.
A predecessor without this protocol cannot be drained with these commands.

## First-time HTTP setup

For each private root (`/state` for `reader`, `/profiles` for `login`,
`/maintenance` for `maintenance`), the owner separately provisions four regular,
single-link files owned by the application UID with mode `0600`. The root itself
must be owned by that UID with mode `0700` and have no symlink in its path:

- `.deployment-lock`, `.deployment-active`, `.deployment-owner`: initially empty.
- `.deployment-control.json`: `{"schema":1,"scope":"reader","operation":"","completed":[],"inflight":[],"frozen":false}`
  with the corresponding scope for the other roots.

For a **new, empty reader root**, run this as the same UID that will run the
application. It creates only deployment records and refuses existing contents:

```sh
install -d -m 0700 "$HOME/.photos-mcp/state"
python3 - "$HOME/.photos-mcp/state" reader <<'PY'
import json
import os
from pathlib import Path
import stat
import sys

root = Path(sys.argv[1]).expanduser().absolute()
scope = sys.argv[2]
if scope not in {"reader", "login", "maintenance"}:
    raise SystemExit("Choose reader, login or maintenance.")
if root.resolve() != root or not root.is_dir():
    raise SystemExit("Use an existing directory without symlinks.")
info = root.stat()
if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
    raise SystemExit("The application UID must own this directory with mode 0700.")
if any(root.iterdir()):
    raise SystemExit("This example requires an empty root; preserve existing state.")
records = {"lock": "", "active": "", "owner": ""}
records["control.json"] = json.dumps({
    "schema": 1, "scope": scope, "operation": "", "completed": [],
    "inflight": [], "frozen": False,
}) + "\n"
for name, content in records.items():
    fd = os.open(root / (".deployment-" + name),
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
PY
```

For a fresh Compose setup, use absolute paths for `./state`, `./profiles` and
`./maintenance` instead, with scopes `reader`, `login` and `maintenance`
respectively. Run the provisioning example separately for each empty root under
its application's UID. Set `PHOTOS_STATE_DIR` to the reader root when running
HTTP outside Docker. Stdio does not require these HTTP deployment records.

## Existing installations and upgrades

Provision only the deployment records; never initialize missing account, profile,
connection, snapshot, maintenance or budget state. Existing control records are
never replaced to resolve a refusal. The completed-operation ledger has a fixed
4096-entry limit and refuses when full; it never silently discards replay history.
Keep these records with their current state during image-only replacement.
The first-time example is deliberately unsuitable for a populated root. An
existing installation without these records needs an independently qualified
adoption procedure before starting a protocol-aware image. Do not make up empty
records to bypass a deployment refusal or claim that an older process is drained.

## Cooperative replacement

Serving processes own a kernel lock and publish a fresh boot UUID. Every admission also writes a durable in-flight ticket before work starts and
removes only that original ticket after all work finishes. A killed process leaves
its ticket in place even if a browser child survives. Such work stays unknown and
busy across restart; there is no automatic cleanup, repair or inference from PID
liveness. Both work tickets and replay history have a 4096-entry bound.

Every ASGI
request, body, response stream and WebSocket holds a work lease. A disconnected
caller leaves any running application work under its original lease until it
actually finishes. Sign-in windows reserve before display creation through
queued runners and final display cleanup; the login keeper covers the entire
refresh, including its final accounting. Reader relay attempts cover unused
links, background polling, final owner binding and natural retirement. All
mutating maintenance CLI commands share the human service's cross-process gate;
a held command refuses before loading maintenance state or credentials.

The local control entry point is `python -m photos_mcp.deployment SCOPE ACTION`.
Actions are `status`, `hold --operation UUID`, `drain --operation UUID --timeout 300`
and `release --operation UUID --boot UUID`. Access to container execution remains
the administrative boundary; no new HTTP route or provider authority is added.
Normal HTTP authentication, signed-person routing and browser owner policy still
apply to requests admitted by the gate.

The controller holds reader admission, then both writer admissions. Existing
reader viewer requests, background relay polling and writer window-state APIs
remain usable while old work finishes. It drains/freezes the reader first, then
the login and maintenance services. New business calls and sign-in attempts are
blocked; existing windows are never cancelled by drain. An unused live link may
keep the reader busy until its original expiry. A timeout retains the operation's
hold and original work; it does not shorten a window's deadline, stop a process,
withdraw a snapshot, or clear a provider/maintenance hold.

Freeze requires zero work under the same admission mutex and denies new work,
including viewer continuations. A successor inherits the same durable hold and
publishes its own boot UUID before serving. Human-service startup skips journal
recovery while held; maintenance CLI work also refuses under the inherited hold.
Only independently verified, accepted successors may be released by a controller.
Release requires the exact held operation and serving boot, appends the operation
to durable replay history and cannot be replayed. A controller may independently
observe `operation == ""`, `last_operation == original` and the same serving boot
to recover a completed release without issuing it again.

Freeze blocks MCP discovery too. The controller must keep its host fence through
post-release authenticated initialization and tools discovery, permitted-person
catalogue checks and anonymous/wrong-person denials. These checks never call a
library tool, sign in, disconnect, enroll an account or change identity grants.
Readiness alone does not qualify that external routing boundary.
