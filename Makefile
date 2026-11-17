# Convenience targets. The firmware's own Makefile is build/Makefile.

DOCKER       ?= docker
DOCKER_IMAGE ?= buck50-build
PYTHON       ?= python3

BINARIES = build/buck50.bin build/buck50.hex build/buck50.elf build/buck50.elf.map

.PHONY: help firmware firmware-image firmware-check \
        test test-host test-viewers test-firmware

help:
	@echo "make firmware        build build/buck50.{elf,bin,hex} in Docker with the"
	@echo "                     pinned toolchain (no ARM compiler needed on host)"
	@echo "make firmware-check  as above from clean, then fail if the result"
	@echo "                     differs from the committed binaries"
	@echo "make test            run all tests (pip install -r test/requirements.txt)"
	@echo "make test-host       buck50.py against a simulated device"
	@echo "make test-viewers    exported files through sigrok-cli, gtkwave"
	@echo "                     tools, gnuplot (those not installed: skipped)"
	@echo "make test-firmware   capture code of build/buck50.elf under emulation"
	@echo "make docs/MANUAL.html  needs multimarkdown"

firmware-image:
	$(DOCKER) build --target toolchain -t $(DOCKER_IMAGE) .

firmware: firmware-image
	$(DOCKER) run --rm -u $$(id -u):$$(id -g) -v $(CURDIR)/build:/src $(DOCKER_IMAGE) make

firmware-check: firmware-image
	$(DOCKER) run --rm -u $$(id -u):$$(id -g) -v $(CURDIR)/build:/src $(DOCKER_IMAGE) make clean all
	git diff --exit-code --stat -- $(BINARIES)

test:
	$(PYTHON) -m pytest test

test-host:
	$(PYTHON) -m pytest test/test_host.py

test-viewers:
	$(PYTHON) -m pytest test/test_viewers.py -rs

test-firmware:
	$(PYTHON) -m pytest test/test_firmware.py

docs/MANUAL.html: docs/MANUAL.md
	multimarkdown -o $@ $<
