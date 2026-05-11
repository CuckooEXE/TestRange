# testrange

Declarative Python plans → VM test-ranges → user test functions.

Status: pre-1.0, under active construction. See `PLAN.md` for the design
and `TODO.md` for in-scope and long-term work.

## Quickstart (when v0 ships)

```sh
pip install -e '.[dev,libvirt,ssh,cloudinit,http]'
testrange cache add https://cloud.debian.org/.../debian-13-generic-amd64.qcow2 \
    --name debian-13
testrange describe examples/hello_world.py
testrange run examples/hello_world.py
```

See `examples/hello_world.py` for the target API shape.
