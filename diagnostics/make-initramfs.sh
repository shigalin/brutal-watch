#!/bin/bash
# Runs in the build container. Output is disposable guest userspace only.
set -euo pipefail
out=$1
module=$2
kernel=$3
mkdir -p "$out"/{bin,sbin,usr/bin,usr/sbin,lib64,proc,sys,dev,tmp,run/netns}
if [[ ${VM_TCP_ONLY:-} == tcp-only ]]; then
    touch "$out/tcp-only"
else
    rm -f "$out/tcp-only"
fi
install -m 755 /bin/busybox "$out/bin/busybox"
ln -sf busybox "$out/bin/sh"
for binary in /usr/sbin/ip /usr/bin/ss; do
    target="$out$binary"
    mkdir -p "$(dirname "$target")"
    cp -L "$binary" "$target"
    while read -r library; do
        mkdir -p "$out$(dirname "$library")"
        cp -L "$library" "$out$library"
    done < <(ldd "$binary" | awk '/=> \// {print $3} /^\s*\// {print $1}')
done
cp -L /lib64/ld-linux-x86-64.so.2 "$out/lib64/ld-linux-x86-64.so.2"
gcc -static -O2 -Wall -Wextra /work/socket_test.c -o "$out/socket-test"
cp "$module" "$out/brutal.ko"
for name in inet_diag tcp_diag veth; do
    source=$(modinfo -k "$kernel" -n "$name")
    case "$source" in
        *.xz) xz -dc "$source" > "$out/$name.ko" ;;
        *.gz) gzip -dc "$source" > "$out/$name.ko" ;;
        *) cp "$source" "$out/$name.ko" ;;
    esac
done
install -m 755 /work/guest-init.sh "$out/init"
(cd "$out" && find . -print0 | cpio --null -o -H newc | gzip -1) > "$out.cpio.gz"
