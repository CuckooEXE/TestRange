# `tools/standup/` — leaked-node certification vehicles

Plans that stand up a **nested hypervisor node** on the local libvirt L0 and
**leak** it (`orch.leak()` as the final TEST), so the node outlives the run and
a backend profile can be pointed at it. This is how a backend gets a live
certification target without dedicated hardware: stand up → leak → run the
`tests/plans/` corpus against the node → `testrange cleanup <run-id>`.

These are *not* certification plans themselves — they deliberately leak
resources, so they must never be part of a corpus sweep. They live here, not in
`examples/` (operator tooling, not API showcase) and not in `tests/plans/`
(the corpus must stay leak-free).

| Plan | Node | Standup path |
|------|------|--------------|
| `libvirt_node.py` | Debian 13 + qemu/libvirt (`GuestHypervisor.libvirt`) | image-origin cloud-init build |
| `esxi_node.py` | ESXi 8 (`GuestHypervisor.esxi`) | installer-origin kickstart (`--build-timeout 1800 --lease-timeout 900`; vmk0 MAC-follow spans two boots, ESXI-18) |
| `pve_node.py` | Proxmox VE 9 (`ProxmoxAnswerBuilder`) | installer-origin answer file (uefi/q35) |

## Workflow

```sh
# 1. stand up + leak (run id is printed; also in `testrange cleanup --list`)
testrange run --profile libvirt-local tools/standup/libvirt_node.py

# 2. point a profile at the node (see below), then certify
for p in tests/plans/generic/*.py; do
    testrange run --profile libvirt-nested "$p" || break
done

# 3. tear the leaked node down
testrange cleanup <run-id>
```

## Profiles for the leaked nodes (gitignored `connect.toml`)

```toml
[libvirt-nested]
driver = "libvirt"
uri = "qemu+ssh://admin@10.66.0.100/system?keyfile=/home/user/.cache/testrange/standup-libvirt.key&no_verify=1&sshauth=privkey"

[libvirt-nested.uplinks]
egress = "tr-egress"        # baked into the node by the standup plan

[esxi-nested]
driver = "esxi"
host = "<address printed by report_node_address>"
user = "root"
password = "TestRangeNested1!"
datastore = "datastore1"

[esxi-nested.uplinks]
egress = "vmnic1"           # the standup's second e1000e NIC

[pve-nested]
driver = "proxmox"
host = "10.55.0.100"
user = "root"
password = "tr-proxmox-lab"

[pve-nested.uplinks]
egress = "vmbr0"            # the installer's mgmt bridge; chained NAT via the standup sidecar
```

The standup credentials use deterministic keys seeded from their comment, so
the `libvirt-nested` keyfile can be re-materialized at any time:

```sh
python -c "from testrange.utils import SSHKey; \
    print(SSHKey.generate(comment='testrange-standup-libvirt').priv, end='')" \
    > ~/.cache/testrange/standup-libvirt.key && chmod 600 ~/.cache/testrange/standup-libvirt.key
```
