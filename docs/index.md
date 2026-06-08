# testrange documentation

```{image} _static/testrange-logo-horizontal.svg
:alt: testrange
:width: 480px
:align: center
```

`testrange` takes a declarative Python `Plan` of VM topologies, brings them
up against a hypervisor, runs user test functions against the live range,
and tears it all down. The aim is a fast, reproducible iteration loop for
CI-style work against specific OS versions and varied network topologies —
including authorized pentest test-ranges.

```{toctree}
:maxdepth: 2
:caption: User guide

user/install
user/drivers/index
user/writing-a-plan
user/connecting-to-a-backend
user/build-vs-run
user/running-tests
```

```{toctree}
:maxdepth: 2
:caption: Developer guide

dev/contributing
dev/architecture
dev/extending/index
dev/bugfixing
```

```{toctree}
:maxdepth: 1
:caption: Reference

adr/index
```

## Building these docs

```sh
pip install -e '.[docs]'
make -C docs html
# open docs/_build/html/index.html
```
