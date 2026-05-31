# ADR-0020: GuestGateway — off-box guest reachability abstraction

Status: Accepted
Date: 2026-05-31

## Context

Communicators reach a guest one of two ways. The **native agent**
(`NativeCommunicator` → QGA / VMware Tools) tunnels through the backend control
plane, so it works regardless of where the orchestrator runs. **SSH**
(`SSHCommunicator`) dials the guest's own IP directly with paramiko.

That direct dial is fine for a **co-located** orchestrator (local libvirt: the
runner is on the hypervisor, guests on its bridges are routable). It breaks for
a **remote** backend: with Proxmox driven over its REST API from a workstation,
the guest sits on an isolated SDN vnet the runner cannot route to. The first
live `capabilities.py` run on Proxmox confirmed this exactly — every
`NativeCommunicator` VM came ready, every `SSHCommunicator` VM failed to connect
to a `10.30.0.x` guest address (ADR-0009 friction #2: "`mgmt`-as-host-adapter
and `mgmt`-as-orchestrator-reachable are the same thing only on-box").

mgmt(B) (ADR-0009, realized for PVE in PVE-44) gives the *hypervisor host* an L2
presence on the guest segment, so the host can reach the guests even when the
runner cannot. What was missing was a way to *use* the host as a stepping stone
without teaching `SSHCommunicator` about Proxmox, or the driver about SSH.

## Decision

Introduce a backend-agnostic **`GuestGateway`** ABC
(`testrange/gateways/base.py`) — an opaque broker that reaches a guest's
`(host, port)` from off-box, modelled on the communicator pattern (an ABC with
no shared constructor; each concrete configured with its own args).

Two consumption shapes, because clients differ:

- `open_socket(host, port) -> socket-like` — for a client that accepts a socket
  object (paramiko's `sock=`). Cheap, no extra local listener, so an SSH retry
  loop calls it per attempt.
- `open_local_forward(host, port) -> int` — binds a local `127.0.0.1` listener
  tunnelling to the target and returns its port, for a client that can only dial
  an address and cannot be handed a socket (e.g. an HTTP client →
  `localhost:<port>`).

The first concrete is **`SSHJumpGateway`** (`gateways/ssh_jump.py`): a generic
SSH ProxyJump (paramiko `direct-tcpip` for `open_socket`; a threaded
listener+pump for `open_local_forward`). It is configured with a plain SSH
endpoint and knows nothing of any backend — a future port-forward, WireGuard, or
vsock gateway implements the same surface and slots in unchanged.

**Wiring respects the stovepipe rule (the orchestrator brokers):**

- The driver ABC gains `guest_gateway() -> GuestGateway | None`, defaulting to
  `None` (guests directly routable — local libvirt, the mock). It knows nothing
  of communicators.
- `ProxmoxDriver.guest_gateway()` returns an `SSHJumpGateway` built from its own
  connection config (the PVE host SSH endpoint, reusing the SFTP host
  credentials). It knows nothing of `SSHCommunicator`.
- `SSHCommunicator.bind(..., gateway=)` accepts an abstract `GuestGateway` and,
  when present, opens its connection over `gateway.open_socket(...)`. It knows
  nothing of Proxmox or SSH-jumping.
- `orchestrator.run_phase.bind_communicators` reads the driver's gateway and
  hands it to the SSH bind — the only place that sees both sides.

## Consequences

- A remote backend's `SSHCommunicator` guests become reachable without any
  change to the communicator or a per-backend special case; native-agent
  transports ignore the gateway entirely (they ride the control plane).
- `host` passed to `bind` stays the guest's own address; the gateway, not the
  communicator, knows how to get there. The retry loop re-opens the socket each
  attempt (a spent channel can't be reused) so it still waits out a slow `sshd`.
- New backends extend reachability by adding a `GuestGateway` concrete and
  returning it from `guest_gateway()`, not by touching communicators.
- The SSH-jump's `.2` reach depends on the hypervisor carrying a host adapter on
  the guest segment — i.e. on mgmt(B) (ADR-0009) being realized — for a switch
  the runner must reach guests on. Single-node / node-pinned only.
- `GatewayError` (`exceptions.py`) marks non-retryable gateway misconfiguration;
  transient transport failures surface as the transport's own exception so a
  caller's retry loop acts on them.
