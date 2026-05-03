"""libvirt-backed virtual network implementation."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from xml.etree import ElementTree as ET

import libvirt

from testrange._logging import get_logger
from testrange.exceptions import NetworkError
from testrange.networks.base import AbstractSwitch, AbstractVirtualNetwork

if TYPE_CHECKING:
    from testrange.orchestrator_base import AbstractOrchestrator

_log = get_logger(__name__)


def _libvirt_conn(context: AbstractOrchestrator) -> libvirt.virConnect:
    """Extract the libvirt ``virConnect`` handle from an orchestrator.

    The libvirt network/VM implementations are only callable against
    the libvirt orchestrator (they'd fail signature checks in mypy
    otherwise), but the abstract method takes a generic
    :class:`AbstractOrchestrator` — this helper is where we do the
    backend cast.
    """
    return context._conn  # type: ignore[attr-defined]


def _mac_for_vm_network(vm_name: str, net_name: str) -> str:
    """Generate a deterministic, stable MAC address for a VM/network pair.

    Uses the QEMU OUI prefix ``52:54:00`` and fills the last three bytes
    from a SHA-256 digest of ``"<vm_name>:<net_name>"``.

    :param vm_name: VM name.
    :param net_name: Logical network name (not the libvirt name).
    :returns: MAC address string in ``xx:xx:xx:xx:xx:xx`` format.
    """
    digest = hashlib.sha256(f"{vm_name}:{net_name}".encode()).digest()
    b = bytearray(6)
    b[0], b[1], b[2] = 0x52, 0x54, 0x00
    b[3], b[4], b[5] = digest[0], digest[1], digest[2]
    return ":".join(f"{x:02x}" for x in b)

class VirtualNetwork(AbstractVirtualNetwork):
    """A libvirt-managed virtual network.

    Networks are scoped to a single test run: the libvirt name includes a
    short run-ID suffix to prevent collisions between concurrent or
    consecutive test runs.

    .. code-block:: python

        VirtualNetwork(
            name="NetA",
            subnet="10.0.50.0/24",
            dhcp=True,
            internet=True,
            dns=True,
        )

    :param name: Logical network name (used for DNS and ``vNIC``
        matching).
    :param subnet: CIDR subnet (e.g. ``'10.0.50.0/24'``).
    :param dhcp: Enable DHCP on the network bridge.
    :param internet: Enable NAT so VMs can reach the internet.
    :param dns: Enable hostname-based DNS resolution within the network.
    """

    _MAX_BRIDGE_LEN = 15
    """Maximum characters allowed in a Linux network interface name."""

    _run_id: str | None
    """Run UUID bound via :meth:`bind_run`; ``None`` until bound."""

    _lv_network: libvirt.virNetwork | None
    """Active libvirt network object; ``None`` before :meth:`start` is called."""

    _vm_entries: list[tuple[str, str, str]]
    """Registered VM entries as ``(vm_name, mac, ip)`` tuples."""

    host_isolated: bool
    """When ``True``, the host has NO IP address on the bridge.

    Models the ESXi-style cross-vSwitch isolation: VMs on the bridge
    can talk L2 to each other, but the libvirt host (and therefore
    the test runner in local-libvirt mode) has no L3 path to any VM
    on the network.  Setting this to ``True`` forces ``dhcp`` /
    ``dns`` / ``internet`` to ``False`` because each of those
    requires libvirt's dnsmasq running on a host-bound bridge IP.

    Use case: tests that need to assert mutual unreachability
    between management and a VM segment, or that exercise the
    "hypervisor can't see the VM" failure mode of the
    :meth:`~testrange.proxy.base.Proxy` abstraction.

    VMs on a ``host_isolated`` network MUST declare an explicit
    ``ip=`` on every :class:`~testrange.devices.vNIC` (no DHCP to
    auto-assign) and use ``communicator='guest-agent'`` (no SSH
    over an unrouted network).
    """

    def __init__(
        self,
        name: str,
        subnet: str,
        dhcp: bool = True,
        internet: bool = False,
        dns: bool = True,
        switch: AbstractSwitch | str | None = None,
        host_isolated: bool = False,
    ) -> None:
        # ``switch=`` is accepted for portable test code but the
        # libvirt backend has no separate switch layer — every
        # network IS its own bridge.  The field is preserved on the
        # instance for inspection but never consumed by the
        # provisioning path.
        if host_isolated and (dhcp or dns or internet):
            raise ValueError(
                f"VirtualNetwork {name!r}: host_isolated=True is "
                "incompatible with dhcp/dns/internet — each of those "
                "requires libvirt's dnsmasq listening on a "
                "host-bound bridge IP, and host_isolated=True is the "
                "explicit promise that the host has no IP on the "
                "bridge.  Pass dhcp=False, dns=False, internet=False "
                "alongside host_isolated=True."
            )
        super().__init__(
            name, subnet, dhcp, internet, dns, switch=switch,
        )
        self.host_isolated = host_isolated
        self._run_id: str | None = None
        self._lv_network: libvirt.virNetwork | None = None
        # vm_name -> (mac, ip) mappings; populated by Orchestrator before start()
        self._vm_entries: list[tuple[str, str, str]] = []  # (vm_name, mac, ip)

    def bind_run(self, run_id: str) -> None:
        """Associate this network with a specific run ID.

        Called by the :class:`~testrange.backends.libvirt.Orchestrator` before
        :meth:`start`.  The run ID is used to make the libvirt network name
        unique.

        Also clears any VM registrations left over from a previous run so
        the same :class:`VirtualNetwork` instance can be re-used across
        orchestrator entries without accumulating stale DHCP reservations.

        :param run_id: UUID string for the current test run.
        """
        self._run_id = run_id
        self._vm_entries.clear()

    def register_vm(self, vm_name: str, ip: str) -> str:
        """Register a VM's deterministic IP and return its MAC address.

        :param vm_name: VM name.
        :param ip: IP address to assign (DHCP reservation or static).
        :returns: The deterministic MAC address for this VM/network pair.
        """
        mac = _mac_for_vm_network(vm_name, self.name)
        self._vm_entries.append((vm_name, mac, ip))
        return mac

    def register_vm_with_mac(self, vm_name: str, mac: str, ip: str) -> None:
        """Register a VM with an externally-computed MAC address.

        Used by the install-phase network, where VMs need a MAC derived
        from ``(vm_name, "__install__")`` instead of ``(vm_name, self.name)``
        so that the install-phase lease is distinct from the test-phase lease.

        :param vm_name: VM name.
        :param mac: Pre-computed MAC address string.
        :param ip: IP address to assign.
        """
        self._vm_entries.append((vm_name, mac, ip))

    def backend_name(self) -> str:
        """Return the libvirt network name (≤ 15 characters).

        Format: ``tr-<net[:6]>-<run[:4]>`` (e.g. ``tr-neta-ab12``).

        :returns: libvirt network name string.
        :raises RuntimeError: If :meth:`bind_run` has not been called yet.
        """
        if self._run_id is None:
            raise RuntimeError("bind_run() must be called before backend_name()")
        prefix = self.name[:6].lower().replace("_", "")
        suffix = self._run_id.replace("-", "")[:4]
        return f"tr-{prefix}-{suffix}"

    def bridge_name(self) -> str:
        """Return the Linux bridge interface name (≤ 15 characters).

        :returns: Bridge name string.
        """
        prefix = self.name[:5].lower().replace("_", "")
        suffix = (self._run_id or "0000").replace("-", "")[:4]
        return f"vbr{prefix}{suffix}"[:self._MAX_BRIDGE_LEN]

    def to_xml(self) -> str:
        """Build and return the libvirt network XML for this network.

        Includes DHCP host reservations and optional DNS ``<host>`` entries
        for all registered VMs.

        :returns: XML string suitable for
            ``virConnect.networkDefineXML()``.
        :raises RuntimeError: If :meth:`bind_run` has not been called.
        """
        net = ET.Element("network")
        ET.SubElement(net, "name").text = self.backend_name()

        if self.internet:
            forward = ET.SubElement(net, "forward", mode="nat")
            nat = ET.SubElement(forward, "nat")
            ET.SubElement(nat, "port", start="1024", end="65535")

        ET.SubElement(
            net,
            "bridge",
            name=self.bridge_name(),
            stp="on",
            delay="0",
        )

        if self.host_isolated:
            # No <ip> on the bridge → libvirt creates the bridge
            # but the host has no IP on it.  Bridge stays L2-only:
            # VMs talk to each other via the bridge, host has zero
            # L3 visibility.  No <dns> either (no dnsmasq target).
            # Constructor already validated dhcp/dns/internet are
            # all False, so the rest of this method's branches
            # short-circuit.
            ET.indent(net)
            return ET.tostring(net, encoding="unicode", xml_declaration=False)

        ip_elem = ET.SubElement(
            net,
            "ip",
            address=self.gateway_ip,
            netmask=self.netmask,
        )

        if self.dhcp:
            dhcp_elem = ET.SubElement(ip_elem, "dhcp")
            ET.SubElement(
                dhcp_elem,
                "range",
                start=self.dhcp_range_start,
                end=self.dhcp_range_end,
            )
            for vm_name, mac, ip in self._vm_entries:
                ET.SubElement(
                    dhcp_elem,
                    "host",
                    mac=mac,
                    name=vm_name,
                    ip=ip,
                )

        if self.dns:
            if self._vm_entries:
                dns_elem = ET.SubElement(net, "dns")
                for vm_name, _mac, ip in self._vm_entries:
                    host_elem = ET.SubElement(dns_elem, "host", ip=ip)
                    # FQDN only: ``<vm>.<network>`` — the network name acts
                    # as a TLD so operators can tell which logical network
                    # a name came from ("webpublic.Internet" vs
                    # "webpublic.Staging"). Bare ``<vm>`` is deliberately
                    # not registered so every cross-VM lookup is explicit.
                    ET.SubElement(host_elem, "hostname").text = (
                        f"{vm_name}.{self.name}"
                    )
        else:
            # Explicitly disable libvirt's dnsmasq DNS (passes --port=0) so it
            # does not bind port 53 on the bridge IP. Without this, libvirt
            # defaults DNS on and collides with a host-level dnsmasq that
            # already owns 0.0.0.0:53.
            ET.SubElement(net, "dns", enable="no")

        ET.indent(net)
        return ET.tostring(net, encoding="unicode", xml_declaration=False)

    def start(self, context: AbstractOrchestrator) -> None:
        """Define and activate the network in libvirt.

        :param context: The libvirt orchestrator; the ``virConnect``
            handle is pulled off its ``_conn`` attribute.
        :raises NetworkError: If the network cannot be defined or started.
        """
        conn = _libvirt_conn(context)
        xml = self.to_xml()
        try:
            self._lv_network = conn.networkDefineXML(xml)
            self._lv_network.setAutostart(True)
            self._lv_network.create()
        except libvirt.libvirtError as exc:
            raise NetworkError(
                f"Failed to start network {self.backend_name()!r}: {exc}"
            ) from exc
        _log.debug(
            "network %r active: bridge=%s subnet=%s internet=%s dns=%s",
            self.backend_name(),
            self.bridge_name(),
            self.subnet,
            self.internet,
            self.dns,
        )

    def stop(self, context: AbstractOrchestrator) -> None:
        """Destroy and undefine this network.

        Safe to call if the network was never started.

        :param context: The libvirt orchestrator.
        """
        conn = _libvirt_conn(context)
        if self._lv_network is not None:
            try:
                if self._lv_network.isActive():
                    self._lv_network.destroy()
                self._lv_network.undefine()
            except libvirt.libvirtError:
                pass  # best-effort teardown
            self._lv_network = None
        else:
            # Attempt lookup by name as a fallback
            lv_name = self.backend_name()
            try:
                net = conn.networkLookupByName(lv_name)
                if net.isActive():
                    net.destroy()
                net.undefine()
            except libvirt.libvirtError:
                pass
