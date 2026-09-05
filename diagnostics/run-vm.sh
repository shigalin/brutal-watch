#!/bin/bash
# QEMU TCG supplies an independent guest kernel. Never loads the module on host.
set -euo pipefail
[[ $(uname -s) == Linux ]] || { echo 'Requires a Linux Docker host'; exit 1; }
module=$(realpath "$1")
name=$2
expected=${3:-pass}
mode=${4:-mptcp}
[[ "$name" =~ ^[a-zA-Z0-9_-]+$ ]] || exit 2
work=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
kernel=$(uname -r)
limits=(--rm --network=none --memory=384m --memory-swap=512m --cpus=0.5
        --pids-limit=64 --cap-drop=ALL --security-opt=no-new-privileges)
image=brutal-watch-vm:377d2a0
docker run "${limits[@]}" -e VM_TCP_ONLY="$mode" -v /lib/modules:/lib/modules:ro -v "$work:/work:rw" \
  -v "$module:/module/brutal.ko:ro" "$image" \
  bash /work/make-initramfs.sh "/work/root-$name" /module/brutal.ko "$kernel"
docker run "${limits[@]}" -v /boot:/boot:ro -v "$work:/work:ro" "$image" \
  timeout -k 2 120 qemu-system-x86_64 -accel tcg,thread=single -cpu max -m 192 \
  -smp 1 -nodefaults -no-reboot -display none -serial stdio -monitor none \
  -kernel "/boot/vmlinuz-$kernel" -initrd "/work/root-$name.cpio.gz" \
  -append 'console=ttyS0 quiet panic=-1 oops=panic rdinit=/init' > "$work/$name.log" 2>&1
if [[ "$expected" == crash ]]; then
  grep -q 'mptcp_stream_accept' "$work/$name.log"
  grep -q 'Kernel panic' "$work/$name.log"
  echo 'EXPECTED_MPTCP_CRASH_REPRODUCED'
else
  grep -q 'VM_ALL_PASS' "$work/$name.log"
  if grep -qE 'Kernel panic|general protection fault|BUG:' "$work/$name.log"; then
    cat "$work/$name.log"
    exit 1
  fi
  grep -E 'SOCKET_TEST_PASS|VM_.*PASS' "$work/$name.log"
fi
