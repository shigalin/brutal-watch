FROM gcc@sha256:3617a214e52a25bde5375dc9503b5e67f01b6c7322a30137e2790aa8e6db5d1f
RUN apt-get update && apt-get install -y --no-install-recommends \
    qemu-system-x86 busybox-static cpio iproute2 kmod \
    && rm -rf /var/lib/apt/lists/*
