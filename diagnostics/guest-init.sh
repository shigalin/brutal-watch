#!/bin/sh
export PATH=/usr/sbin:/usr/bin:/bin:/sbin
/bin/busybox --install -s /bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
set -e
tcp_mode=
[ ! -f /tcp-only ] || tcp_mode=tcp-only
/usr/sbin/ip link set lo up
insmod /veth.ko
/usr/sbin/ip netns add client
/usr/sbin/ip link add s0 type veth peer name c0
/usr/sbin/ip link set c0 netns client
/usr/sbin/ip link set s0 up
/usr/sbin/ip -n client link set lo up
/usr/sbin/ip -n client link set c0 up
/usr/sbin/ip addr add 192.0.2.1/24 dev s0
/usr/sbin/ip -n client addr add 192.0.2.2/24 dev c0
/usr/sbin/ip -6 addr add fd00::1/64 dev s0 nodad
/usr/sbin/ip -n client -6 addr add fd00::2/64 dev c0 nodad
insmod /brutal.ko
insmod /inet_diag.ko
insmod /tcp_diag.ko
echo 'add 192.0.2.2/32 rate=12500000 gain=20 lock' > /proc/net/tcp_brutal/rules
echo 'add fd00::2/128 rate=12500000 gain=20 lock' > /proc/net/tcp_brutal/rules
/usr/sbin/ip route add 192.0.2.2/32 dev s0 congctl lock brutal
/usr/sbin/ip -6 route add fd00::2/128 dev s0 congctl lock brutal
echo 'VM_RULES_ENABLED'
/socket-test 4 1 8 "$tcp_mode"
/socket-test 6 1 8 "$tcp_mode"
timeout 5 /usr/bin/ss -Htin >/tmp/diag
echo 'VM_DIAG_PASS'
echo 'add 192.0.2.2/32 rate=6250000 gain=20 lock' > /proc/net/tcp_brutal/rules
cat /proc/net/tcp_brutal/rules
/socket-test 4 1 2 "$tcp_mode"
/usr/sbin/ip route del 192.0.2.2/32 dev s0
/usr/sbin/ip -6 route del fd00::2/128 dev s0
echo 'del 192.0.2.2/32' > /proc/net/tcp_brutal/rules
echo 'del fd00::2/128' > /proc/net/tcp_brutal/rules
/socket-test 4 0 2 "$tcp_mode"
/socket-test 6 0 2 "$tcp_mode"
timeout 5 /usr/bin/ss -Htin >/tmp/diag
echo 'VM_ALL_PASS'
poweroff -f
