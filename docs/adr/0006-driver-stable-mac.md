# ADR-0006: Stable MAC assignment is a driver concern

Status: Accepted
Date: 2026-05-11

## Context

The cached post-install disk has cloud-init's rendered
``/etc/netplan/50-cloud-init.yaml`` baked in. If cloud-init's
network rendering keys interfaces by MAC (common on Debian/Ubuntu
cloud images), and the run VM gets a different MAC than the install
VM, netplan's ``match: macaddress: ...`` stanza fails silently — the
interface comes up unconfigured.

The defensive measure is a stable MAC derived deterministically from
the plan + VM + NIC index, so install and run VM get the same MAC.

## Decision

- ``HypervisorDriver.compose_mac(plan_name, vm_name, nic_idx) -> str``
  on the ABC.
- Each driver picks its own OUI (libvirt/KVM ``52:54:00:``, VMware
  ``00:50:56:``, Hyper-V ``00:15:5D:``, etc.) and its own MAC-format
  conventions.
- LibvirtDriver hashes ``"{plan_name}/{vm_name}/{nic_idx}"`` with
  SHA-256 and uses the first 3 bytes after the OUI prefix.

The ``CloudInitBuilder`` network-config render also matches by
interface name (``match: name: en*``) as belt-and-suspenders.

## Consequences

- This belongs in the driver, not in shared utility code, because
  OUI choice and MAC-format conventions vary per backend.
- Required prerequisite for the future "static IP via DHCP host
  reservation" feature, which keys on MAC.
