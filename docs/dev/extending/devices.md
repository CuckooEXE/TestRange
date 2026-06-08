# Adding a device kind

Devices are Plan-time dataclasses attached to a `VMSpec`. They are
**data** — they don't drive the backend. Drivers consume the VMSpec
and translate its devices into backend-specific XML / API calls.

The layout under `testrange/devices/`:

```
devices/
├── __init__.py         # re-exports the generic shapes
├── base.py             # Device ABC
├── cpu/base.py         # CPU dataclass
├── memory/base.py      # Memory dataclass
├── disk/base.py        # OSDrive, HardDrive dataclasses
├── network/base.py     # NetworkIface, DHCPAddr, StaticAddr
└── pool/base.py        # StoragePool dataclass
```

`base.py` per kind holds the **generic** shape. When a backend needs a
driver-specific knob on a device, the variant lives in a driver-named file under
the same package — e.g. `devices/disk/libvirt.py` (`LibvirtOSDrive`,
`LibvirtDataDrive`) and `devices/network/libvirt.py` (`LibvirtNetworkIface`),
which expose libvirt bus/model strings the generic shapes don't carry.

## Generic vs driver-specific

If your new device has the same shape on every backend (a count, a
size in MB, a bool flag), put it in the appropriate `base.py`:

```python
# devices/widget/base.py
from dataclasses import dataclass
from testrange.devices.base import Device

@dataclass(frozen=True)
class Widget(Device):
    count: int

    def __post_init__(self) -> None:
        if not isinstance(self.count, int) or self.count < 1:
            raise ValueError(f"Widget.count must be a positive int, got {self.count!r}")
```

Validation goes in `__post_init__`. This is a trust boundary — users
construct these directly in their Plan files where mypy isn't in the
loop, so type/value checks here are non-negotiable.

Re-export it from `testrange/devices/__init__.py` so users can
`from testrange.devices import Widget`.

## Driver-specific knobs

If the device needs to expose a backend-specific knob (e.g. a NIC model
string a particular backend understands), subclass the generic dataclass in a
driver-named file:

```python
# devices/widget/proxmox.py
from dataclasses import dataclass
from testrange.devices.widget.base import Widget

@dataclass(frozen=True)
class ProxmoxWidget(Widget):
    model: str = "default"

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.model, str) or not self.model:
            raise ValueError("ProxmoxWidget.model must be a non-empty string")
```

**Do NOT** re-export driver-specific variants from
`testrange/devices/__init__.py` — that would leak driver-specific
naming into the generic namespace. Users import them from the
driver-named path:

```python
from testrange.devices.widget.proxmox import ProxmoxWidget
```

The shipped precedent is `devices/disk/libvirt.py`: when a knob is shared across
two generic shapes (`OSDrive` and `HardDrive`), factor it into a private
intermediate (`_LibvirtDisk`) and pull it in by multiple inheritance —
`class LibvirtOSDrive(_LibvirtDisk, OSDrive)` — rather than duplicating the
field and its validation across both concretes.

## Adding to `VMSpec`

`VMSpec` enforces singleton-device invariants (exactly one CPU, one
Memory, one OSDrive). If your device should be singleton-required or
singleton-optional, extend the checks in `testrange/vms/spec.py`.
Otherwise, devices flow into `spec.devices` and drivers can filter
them by `isinstance`:

```python
widgets = [d for d in spec.devices if isinstance(d, Widget)]
```

## Tests

`tests/unit/test_devices.py` is the home for device validation
tests. Cover the failure modes: bad type, bad value, missing required.
Skip pure type-system tests (a `@dataclass(frozen=True)` test that
just checks the decorator works isn't worth its weight).
