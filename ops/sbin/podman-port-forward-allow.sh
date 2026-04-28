#!/bin/sh
set -eu
OLD_COMMENT=nanobot-podman-public-ports
RSS_COMMENT=nanobot-podman-rss-public-port
DENY_COMMENT=nanobot-old-public-ports-closed
OLD_PUBLIC_PORTS=8000,8080,8081,8091,8092,8094
PUBLIC_IFACE=${NANOBOT_PUBLIC_IFACE:-eth0}

# Remove legacy direct Podman public forwards. Public HTTP now enters through 8093.
while iptables -C FORWARD -p tcp -d 172.17.0.0/16 -m multiport --dports 8091,18790 -m comment --comment "$OLD_COMMENT" -j ACCEPT 2>/dev/null; do
  iptables -D FORWARD -p tcp -d 172.17.0.0/16 -m multiport --dports 8091,18790 -m comment --comment "$OLD_COMMENT" -j ACCEPT
done
while iptables -C FORWARD -p tcp -d 172.17.0.0/16 --dport 8091 -m comment --comment "$RSS_COMMENT" -j ACCEPT 2>/dev/null; do
  iptables -D FORWARD -p tcp -d 172.17.0.0/16 --dport 8091 -m comment --comment "$RSS_COMMENT" -j ACCEPT
done

# Close old public sidecar ports on the external interface only.
# Loopback and container bridge traffic remain available for internal calls.
while iptables -C INPUT -i "$PUBLIC_IFACE" -p tcp -m multiport --dports "$OLD_PUBLIC_PORTS" -m comment --comment "$DENY_COMMENT" -j DROP 2>/dev/null; do
  iptables -D INPUT -i "$PUBLIC_IFACE" -p tcp -m multiport --dports "$OLD_PUBLIC_PORTS" -m comment --comment "$DENY_COMMENT" -j DROP
done
iptables -I INPUT 1 -i "$PUBLIC_IFACE" -p tcp -m multiport --dports "$OLD_PUBLIC_PORTS" -m comment --comment "$DENY_COMMENT" -j DROP
