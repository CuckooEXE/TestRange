"""``resolve_build_switch`` — the build-switch synthesis seam (ADR-0016).

Folds a user-declared build switch (``Switch | None``) into the concrete
``Switch`` the build phase brings up. Egress is out-of-band now, so there is no
managed-egress carrier — a build switch is just an ordinary ``Switch``:

- ``None``   — the default isolated build switch (DHCP+DNS, **no** egress).
- ``Switch`` — honored as declared (BYO; sidecar may even be ``None``), realized
  exactly like a run-phase switch.
"""

from __future__ import annotations

from testrange.networks import Network, Sidecar, Switch
from testrange.orchestrator.build import (
    BUILD_CIDR,
    BUILD_NETWORK_NAME,
    BUILD_SWITCH_NAME,
    resolve_build_switch,
)


class TestResolveNone:
    def test_default_is_isolated_dhcp_dns_no_egress(self) -> None:
        # ADR-0016: no declared build switch => no internet egress. The default
        # is the DHCP+DNS internal switch, no uplink, no NAT.
        switch = resolve_build_switch(None)
        assert switch.name == BUILD_SWITCH_NAME
        assert switch.cidr == BUILD_CIDR
        assert switch.uplink is None
        assert switch.networks[0].name == BUILD_NETWORK_NAME
        assert switch.sidecar is not None
        assert (switch.sidecar.dhcp, switch.sidecar.dns, switch.sidecar.nat) == (True, True, False)


class TestResolvePlainSwitch:
    def test_passthrough_identity(self) -> None:
        # A NAT egress build switch routes out a named (out-of-band) uplink, just
        # like a run-phase switch.
        declared = Switch(
            "custom",
            Network("n"),
            cidr="10.5.0.0/24",
            uplink="egress",
            sidecar=Sidecar(dhcp=True, dns=True, nat=True),
        )
        assert resolve_build_switch(declared) is declared  # honored exactly as declared

    def test_sidecarless_switch_honored(self) -> None:
        # A builder that carries its own static L3 (BUILD-1/BUILD-2) is allowed
        # a bare build switch; resolve must not synthesize a sidecar onto it.
        declared = Switch("custom", Network("n"), cidr="10.5.0.0/24")
        switch = resolve_build_switch(declared)
        assert switch is declared
        assert switch.sidecar is None
