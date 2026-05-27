"""Tests for the sidecar config ISO builder."""

from __future__ import annotations

import io

import pycdlib

from testrange.builders.sidecar_iso import (
    SIDECAR_CONFIG_LABEL,
    build_sidecar_config_iso,
)


def _read_iso(data: bytes) -> dict[str, bytes]:
    iso = pycdlib.PyCdlib()
    iso.open_fp(io.BytesIO(data))
    files: dict[str, bytes] = {}
    for joliet in ("/dnsmasq.conf", "/interfaces", "/nftables.nft", "/sysctl.conf"):
        buf = io.BytesIO()
        iso.get_file_from_iso_fp(buf, joliet_path=joliet)
        files[joliet] = buf.getvalue()
    iso.close()
    return files


def test_iso_round_trips_all_four_files() -> None:
    data = build_sidecar_config_iso(
        dnsmasq_conf="# dnsmasq\nport=0\n",
        interfaces="auto eth0\niface eth0 inet static\n",
        nftables_ruleset="flush ruleset\n",
        sysctl_conf="net.ipv4.ip_forward=1\n",
    )
    assert data[0x8001:0x8006] == b"CD001"
    files = _read_iso(data)
    assert files["/dnsmasq.conf"] == b"# dnsmasq\nport=0\n"
    assert files["/interfaces"] == b"auto eth0\niface eth0 inet static\n"
    assert files["/nftables.nft"] == b"flush ruleset\n"
    assert files["/sysctl.conf"] == b"net.ipv4.ip_forward=1\n"


def test_iso_volume_label_is_pinned() -> None:
    data = build_sidecar_config_iso(
        dnsmasq_conf="", interfaces="", nftables_ruleset="", sysctl_conf=""
    )
    iso = pycdlib.PyCdlib()
    iso.open_fp(io.BytesIO(data))
    label = iso.pvd.volume_identifier.decode("ascii").strip()
    iso.close()
    assert label == SIDECAR_CONFIG_LABEL
