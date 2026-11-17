"""Proxy-aware request facts: the real client IP, the real scheme and the Host check.

Behind a Cloudflare Tunnel the TCP peer is the local ``cloudflared`` process, so ``REMOTE_ADDR`` is the proxy, not the
person. ``CF-Connecting-IP`` and ``X-Forwarded-Proto`` are honoured ONLY when the TCP peer is inside the configured
``trusted_proxies`` (CIDRs). From any other peer those headers are attacker-controlled text and are ignored.
"""
from __future__ import annotations

import ipaddress
import json

CLIENT_IP_HEADER = "HTTP_CF_CONNECTING_IP"
PROTO_HEADER = "HTTP_X_FORWARDED_PROTO"
VISITOR_HEADER = "HTTP_CF_VISITOR"           # Cloudflare: {"scheme":"https"}


def parse_networks(cidrs) -> tuple:
    """CIDR strings to networks. A bad entry raises: a typo must not silently mean 'trust nobody' or 'everybody'."""
    return tuple(ipaddress.ip_network(str(c).strip(), strict=False) for c in cidrs if str(c).strip())


def _ip(value: str):
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def peer_is_trusted(peer: str, nets) -> bool:
    ip = _ip(peer)
    return bool(ip and any(ip in n for n in nets if n.version == ip.version))


def client_ip(environ: dict, nets) -> str:
    """The TCP peer, or CF-Connecting-IP when (and only when) the peer is a trusted proxy and the header is a valid IP."""
    peer = environ.get("REMOTE_ADDR", "")
    if peer_is_trusted(peer, nets):
        claimed = _ip(environ.get(CLIENT_IP_HEADER, ""))
        if claimed:
            return str(claimed)
    return peer


def untrusted_proxy_headers(environ: dict, nets) -> bool:
    """Cloudflare headers arrived from a peer that is NOT a trusted proxy: either someone is spoofing them, or the tunnel
    is running without ``--trusted-proxy`` (every visitor would then share one IP). Callers answer 400."""
    present = bool(environ.get(CLIENT_IP_HEADER) or environ.get(VISITOR_HEADER))
    return present and not peer_is_trusted(environ.get("REMOTE_ADDR", ""), nets)


def is_https(environ: dict, nets) -> bool:
    if environ.get("wsgi.url_scheme") == "https":
        return True
    if not peer_is_trusted(environ.get("REMOTE_ADDR", ""), nets):
        return False
    if environ.get(PROTO_HEADER, "").split(",")[0].strip().lower() == "https":
        return True
    try:
        return json.loads(environ.get(VISITOR_HEADER) or "{}").get("scheme") == "https"
    except (ValueError, AttributeError):
        return False


def normalize_host(host: str) -> str:
    return (host or "").strip().lower().rstrip(".")


def host_allowed(host: str, allowed) -> bool:
    """``allowed`` is None (no check) or a collection of lowercase hosts. ``[::1]:8780`` is a valid form; a Host with a
    userinfo, path or whitespace is refused outright."""
    if allowed is None:
        return True
    h = normalize_host(host)
    if not h or any(c in h for c in "/@\\ \t"):
        return False
    return h in allowed


def loopback_hosts(port: int) -> tuple[str, ...]:
    return tuple(f"{h}:{port}" for h in ("127.0.0.1", "localhost", "[::1]"))


def hostname(host: str) -> str:
    """The name part of a Host / netloc value, port and IPv6 brackets removed."""
    h = normalize_host(host)
    if h.startswith("["):
        return h[1:].split("]")[0]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h
