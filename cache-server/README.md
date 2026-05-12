# testrange cache-server

A dumb, path-served HTTP cache for testrange. Sits behind your test range
on a private LAN, holds the heavy artifacts (base images, post-install
disk snapshots) so a fresh machine doesn't have to redownload or rebuild
them. Configuration is one nginx file; storage is one filesystem tree.

## What it is

An nginx container exposing the WebDAV `PUT` / `DELETE` verbs on top of a
plain static-file root. The URL path maps 1:1 to a file under
`/srv/cache`:

| URL                       | File on the server                |
| ------------------------- | --------------------------------- |
| `GET /isos/<sha>.bin`     | `/srv/cache/isos/<sha>.bin`       |
| `PUT /isos/<sha>.bin`     | (uploads)                         |
| `DELETE /isos/<sha>.bin`  | (removes)                         |
| `GET /isos/<sha>.json`    | `/srv/cache/isos/<sha>.json`      |
| `GET /names/<name>`       | `/srv/cache/names/<name>`         |

That's it. No application code. No auth. No quotas. No listing protocol.

testrange's `HttpCache` (`testrange/cache/http.py`) speaks this dialect.
On a local cache miss the client falls through to the server; on a local
write the client mirrors the bin/sidecar/names tuple back. See
`docs/user/install.md` for the wire-up on the client side.

## Quick start

```sh
# 1) Generate a self-signed cert.
mkdir -p certs
openssl req -x509 -newkey rsa:4096 -nodes \
        -keyout certs/server.key -out certs/server.crt \
        -days 365 -subj "/CN=cache.local"

# 2) Bring the server up.
docker compose up -d

# 3) Point testrange at it.
testrange --cache https://localhost:8443 cache push debian-13   # mirror an existing local entry
testrange cache list                                            # local-only
```

The `--cache` flag is the only knob — no env var. That keeps every
`testrange` invocation self-describing: shell history records the cache
URL the run actually used, and you can't accidentally hit a stale cache
because something exported the variable in your `.zshrc` last month.

Plain `https://localhost:8443` works without a CA bundle on the client —
`testrange` never verifies the server certificate. The expectation is
that the server lives on a private network where TLS is for transport
hygiene rather than identity.

## Storage layout

```
/srv/cache/
├── isos/
│   ├── <sha256>.bin        # opaque content
│   └── <sha256>.json       # sidecar (sha256, size, names[], origin, ...)
└── names/
    └── <name>              # text file whose body is the sha it aliases
```

A name resolves with two HTTP round trips: `GET /names/<n>` returns the
sha as plain text; `GET /isos/<sha>.json` returns the sidecar. The
client writes the sidecar **last** so a half-uploaded entry stays
invisible. It deletes names **first** so a stale pointer never references
a missing sidecar.

## Configuration knobs

`nginx.conf` ships with two opinionated values:

| Knob                   | Default        | Why                                          |
| ---------------------- | -------------- | -------------------------------------------- |
| `client_max_body_size` | `16G`          | Comfortable for qcow2 base images.           |
| `ssl_protocols`        | `TLSv1.2 1.3`  | Old enough to be universal, new enough to be safe. |

Edit `nginx.conf` and `docker compose restart` to change either.

## Security caveats

This server is intentionally dumb. Before you put it on a network you
don't fully control, **add a gate in front of it**:

- **No authentication.** Anyone who can reach the port can `PUT` a
  poisoned image at a known sha or `DELETE` your debian-13 mirror.
- **No rate limit.** A misbehaving client can fill your disk.
- **No quota.** Storage is bounded by whatever the host filesystem has.
- **No content verification.** The server doesn't hash uploads; it
  trusts the client to put the right bytes at `<sha>.bin`. The
  testrange client recomputes the sha on add and would catch a swap
  *for entries it ingested itself* — but a pre-poisoned cache is the
  threat model you'd protect against with mTLS or a signed sidecar
  scheme, not with this server.

A reasonable production posture: front this with a reverse proxy that
terminates mTLS, or that checks a bearer token via `auth_request`. The
testrange client deliberately ships no auth knob today — when that
arrives it will be a header injected by the reverse proxy, not by the
client.

## When to use it

- **Multi-host test labs.** One cache server, every libvirt host
  shares the same base images. Saves the second host from a 2-hour
  Debian install.
- **CI.** Pre-warm the cache on a runner; teardown only blows away the
  ephemeral state, not the heavy artifacts.
- **Bandwidth-bound networks.** A local cache server downstream of a
  metered uplink.

If you only have one machine, the local cache alone is enough.
