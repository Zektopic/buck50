# Build environment for the buck50 firmware.
#
# Pins the exact compiler the committed build/buck50.{bin,hex,elf} were
# built with (GNU Arm Embedded Toolchain 9-2020-q2-update, GCC 9.3.1), so
# that "make firmware" reproduces them byte for byte.  See "Building from
# source" in README.md.

FROM ubuntu:24.04

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        bzip2 ca-certificates curl gawk make \
 && apt-get clean

ARG GCC_ARM_URL=https://armkeil.blob.core.windows.net/developer/Files/downloads/gnu-rm/9-2020q2/gcc-arm-none-eabi-9-2020-q2-update-x86_64-linux.tar.bz2
ARG GCC_ARM_SHA256=5adc2ee03904571c2de79d5cfc0f7fe2a5c5f54f44da5b645c17ee57b217f11f

# download to a tmpfs so the archive never lands in an image layer
RUN --mount=type=tmpfs,target=/download \
    curl -fsSL -o /download/gcc-arm.tar.bz2 "$GCC_ARM_URL" \
 && echo "$GCC_ARM_SHA256  /download/gcc-arm.tar.bz2" | sha256sum -c - \
 && mkdir -p /opt/gcc-arm-none-eabi \
 && tar -xjf /download/gcc-arm.tar.bz2 -C /opt/gcc-arm-none-eabi --strip-components=1

ENV GCC_ARM_ROOT=/opt/gcc-arm-none-eabi

WORKDIR /src
CMD ["make"]
