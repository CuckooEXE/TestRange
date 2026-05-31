"""Config-ISO builder for the per-Switch sidecar VM.

The sidecar is a pre-built Alpine image with ``dnsmasq``, ``nftables``,
and ``qemu-guest-agent`` baked in — no cloud-init. Its per-run config is
delivered as a tiny ISO9660 image with volume label ``TR_SIDECAR_CFG``
carrying four files at the root:

- ``dnsmasq.conf``    — rendered by :func:`testrange.networks.sidecar.render_dnsmasq_conf`
- ``interfaces``      — rendered by :func:`testrange.networks.sidecar.render_sidecar_interfaces`
- ``nftables.nft``    — rendered by :func:`testrange.networks.sidecar.render_nftables_ruleset`
- ``sysctl.conf``     — rendered by :func:`testrange.networks.sidecar.render_sysctl_conf`

The sidecar image ships an OpenRC hook that mounts the ISO by label,
copies those files into ``/etc/dnsmasq.conf``, ``/etc/network/interfaces``,
``/etc/nftables.nft``, and ``/etc/sysctl.d/99-testrange.conf``, then
brings interfaces up, applies sysctl, loads nftables, and starts dnsmasq.
The label + four filenames are the contract between this builder and the
sidecar image build (see ``tools/build-sidecar-image/build.sh``).
"""

from __future__ import annotations

import io
from typing import Any

from testrange.exceptions import BuilderError

SIDECAR_CONFIG_LABEL = "TR_SIDECAR_CFG"


def _import_pycdlib() -> Any:
    """Lazy import. Raises BuilderError with a useful hint if pycdlib is missing."""
    try:
        import pycdlib
    except ImportError as e:
        raise BuilderError(
            "pycdlib is not installed; install with `pip install -e .[cloudinit]`"
        ) from e
    return pycdlib


def build_sidecar_config_iso(
    dnsmasq_conf: str,
    interfaces: str,
    nftables_ruleset: str,
    sysctl_conf: str,
) -> bytes:
    """Build the sidecar's config ISO as bytes.

    Returns the ISO9660 image so the orchestrator can upload it to a pool
    the same way it uploads a cloud-init seed.

    Note: the returned bytes are **not** reproducible — pycdlib stamps the
    wall-clock into the volume descriptor, so two calls with identical inputs
    differ by a few bytes. That is harmless because the cache keys on the
    rendered *text* (via ``config_hash`` with ``sort_keys=True``), never on the
    ISO bytes; do not content-address this output.
    """
    pycdlib = _import_pycdlib()

    files = [
        ("/DNSMASQ.;1", dnsmasq_conf.encode("utf-8"), "dnsmasq.conf", "/dnsmasq.conf"),
        ("/INTERFAC.;1", interfaces.encode("utf-8"), "interfaces", "/interfaces"),
        ("/NFTABLES.;1", nftables_ruleset.encode("utf-8"), "nftables.nft", "/nftables.nft"),
        ("/SYSCTL.;1", sysctl_conf.encode("utf-8"), "sysctl.conf", "/sysctl.conf"),
    ]

    iso = pycdlib.PyCdlib()
    # interchange_level=3: ISO9660 with long/relaxed filenames.
    # joliet=3: Joliet extension level 3 (Windows-style long names; harmless
    #   on Linux, lets the guest mount by the friendly names too).
    # rock_ridge="1.09": the Rock Ridge version Linux mounts cleanly — gives
    #   POSIX names/permissions so the sidecar reads /dnsmasq.conf etc. as-is.
    # These mirror the cloud-init seed ISO (see builders/cloudinit.py).
    iso.new(
        interchange_level=3,
        joliet=3,
        rock_ridge="1.09",
        vol_ident=SIDECAR_CONFIG_LABEL,
    )
    for path, data, rr_name, joliet_path in files:
        iso.add_fp(io.BytesIO(data), len(data), path, rr_name=rr_name, joliet_path=joliet_path)

    buf = io.BytesIO()
    iso.write_fp(buf)
    iso.close()
    return buf.getvalue()


__all__ = ["SIDECAR_CONFIG_LABEL", "build_sidecar_config_iso"]
