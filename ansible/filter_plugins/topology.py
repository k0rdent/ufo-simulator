"""Topology helpers for ufo-simulator ansible playbooks."""

import re


def _breakout_port_name(port_prefix, phys, lane):
    """Format a breakout port name for the active NOS/prefix.

    ``lane`` is 1-based in topology math.
    Cumulus (prefix ``swp``) uses ``swp1s0``, ``swp1s1`` (0-based subports).
    Verity (prefix ``eth1/``) keeps slash form ``eth1/1/1``.
    """
    if not port_prefix or port_prefix.rstrip("/").endswith("swp") or port_prefix == "swp":
        return "%s%ds%d" % (port_prefix, phys, lane - 1)
    return "%s%d/%d" % (port_prefix, phys, lane)


def _port_is_breakout(port, port_prefix):
    """True when port uses breakout naming (e.g. eth1/1/1, swp1/1, or swp1s0)."""
    if not isinstance(port, str):
        return False
    rest = port[len(port_prefix) :] if port_prefix and port.startswith(port_prefix) else port
    if "/" in rest:
        return True
    # Cumulus breakout: <phys>s<subport> (e.g. 1s0, 27s0)
    return re.search(r"^\d+s\d+$", rest) is not None


def _is_swp_prefix(port_prefix):
    return (not port_prefix) or port_prefix == "swp" or port_prefix.rstrip("/").endswith("swp")


def expand_switch_port_nics(
    wired_links,
    port_prefix="swp",
    ports_count=48,
    breakout_lanes=1,
    switch_index=0,
    stub_udp_base=40000,
):
    """Pad leaf/spine data NICs to a full physical port grid.

    Cumulus VX + rename-swp.service rename NICs by MAC after boot. If we only
    create NICs for live topology links, high ports like swp27s0 get packed into
    early slots (e.g. swp6s0). Always creating ``ports_count`` physical ports
    (× lanes) keeps Netris/UFO port names aligned with host OS names.

    ``breakout_lanes=2`` → swp1s0, swp1s1, … swp48s0, swp48s1 (96 NICs).
    ``breakout_lanes=1`` → swp1 … swp48.
    """
    if not _is_swp_prefix(port_prefix):
        return list(wired_links or [])

    ports_count = int(ports_count or 48)
    breakout_lanes = max(1, int(breakout_lanes or 1))
    switch_index = int(switch_index or 0)
    stub_udp_base = int(stub_udp_base or 40000)

    by_port = {}
    for link in wired_links or []:
        port = link.get("port")
        if port:
            by_port[port] = dict(link)

    nics = []
    slot = 0
    for phys in range(1, ports_count + 1):
        for lane in range(breakout_lanes):
            if breakout_lanes > 1:
                name = "%s%ds%d" % (port_prefix, phys, lane)
            else:
                name = "%s%d" % (port_prefix, phys)
            if name in by_port:
                nic = dict(by_port[name])
                nic["port"] = name
                nic["stub"] = False
            else:
                # Unique unused UDP pair so libvirt still creates the NIC.
                local = stub_udp_base + (
                    switch_index * (ports_count * breakout_lanes + 8) + slot
                ) * 2
                nic = {
                    "port": name,
                    "udp_local": local,
                    "udp_remote": local + 1,
                    "stub": True,
                }
            nics.append(nic)
            slot += 1
    return nics


def ew_breakout_server_links(leafs, nodes, port_prefix="swp", eth_base=5, breakout=2):
    """Build EW leaf→server links with N-way breakout port names.

    Even node indices share odd physical ports; odd indices share even ports.
    Example (2-way, Cumulus)::

        leaf-0 swp1s0 → node-0 ew nic-0
        leaf-0 swp1s1 → node-2 ew nic-0
        leaf-0 swp2s0 → node-1 ew nic-0
        leaf-0 swp2s1 → node-3 ew nic-0
        leaf-1 swp1s0 → node-0 ew nic-1
        ...

    VM NICs: leaf index L uses eth{eth_base+L} (ew nic-L).
    Links are ordered by physical port then lane for stable NIC attach order.
    """
    if not leafs:
        return []

    indexed = []
    for node in nodes or []:
        name = node["name"] if isinstance(node, dict) else node
        indexed.append((int(str(name).rsplit("-", 1)[-1]), name))
    indexed.sort()

    even = [(i, n) for i, n in indexed if i % 2 == 0]
    odd = [(i, n) for i, n in indexed if i % 2 == 1]

    def assignments_for(group, first_phys):
        out = []
        for j, (_idx, name) in enumerate(group):
            phys = first_phys + (j // breakout) * 2
            lane = (j % breakout) + 1
            out.append((phys, lane, name))
        return out

    assignments = assignments_for(even, 1) + assignments_for(odd, 2)
    assignments.sort(key=lambda item: (item[0], item[1]))

    links = []
    for leaf_i, leaf in enumerate(leafs):
        leaf_name = leaf["name"] if isinstance(leaf, dict) else leaf
        eth = "eth%d" % (eth_base + leaf_i)
        role = "ew%d" % (leaf_i + 1)
        for phys, lane, node_name in assignments:
            links.append(
                {
                    "local": leaf_name,
                    "local_port": _breakout_port_name(port_prefix, phys, lane),
                    "remote": node_name,
                    "remote_port": eth,
                    "role": role,
                }
            )
    return links


def ew_fabric_links(spines, leafs, port_prefix="swp", leaf_uplink_base=27):
    """Build EW spine↔leaf fabric links using breakout lane 1 on each physical port.

    Spine N uses physical ports 1..len(leafs), lane 1 (Cumulus: swp1s0).
    Leaf uplink for spine index S is physical port leaf_uplink_base+S, lane 1
    (Cumulus: swp27s0 / swp28s0).
    """
    if not spines or not leafs:
        return []

    links = []
    for spine_i, spine in enumerate(spines):
        spine_name = spine["name"] if isinstance(spine, dict) else spine
        leaf_phys = leaf_uplink_base + spine_i
        for leaf_i, leaf in enumerate(leafs):
            leaf_name = leaf["name"] if isinstance(leaf, dict) else leaf
            links.append(
                {
                    "local": spine_name,
                    "local_port": _breakout_port_name(port_prefix, leaf_i + 1, 1),
                    "remote": leaf_name,
                    "remote_port": _breakout_port_name(port_prefix, leaf_phys, 1),
                }
            )
    return links


def resolve_topology_link_ports(links, switches, port_prefix="swp"):
    """Remap switch-side ports to sequential names matching virtio NIC order.

    Non-breakout ports are renumbered to sequential swpN for older layouts.
    Breakout ports (e.g. swp1s0 / swp27s0) are preserved; leaf/spine VMs pad to
    a full port grid via expand_switch_port_nics so high ports keep those names.

    Server/softgate ports (eth*) are left unchanged.
    """
    if not links:
        return []

    switch_names = set()
    for sw in switches or []:
        if isinstance(sw, dict):
            name = sw.get("name")
            if name:
                switch_names.add(name)
        elif sw:
            switch_names.add(sw)

    # (switch, declared_port) -> actual_port
    port_map = {}
    for sw in switch_names:
        n = 1
        for link in links:
            if link.get("local") == sw:
                declared = link["local_port"]
                key = (sw, declared)
                if key not in port_map:
                    if _port_is_breakout(declared, port_prefix):
                        port_map[key] = declared
                    else:
                        port_map[key] = "%s%s" % (port_prefix, n)
                    n += 1
        for link in links:
            if link.get("remote") == sw:
                declared = link["remote_port"]
                key = (sw, declared)
                if key not in port_map:
                    if _port_is_breakout(declared, port_prefix):
                        port_map[key] = declared
                    else:
                        port_map[key] = "%s%s" % (port_prefix, n)
                    n += 1

    resolved = []
    for link in links:
        new_link = dict(link)
        local_key = (link.get("local"), link.get("local_port"))
        remote_key = (link.get("remote"), link.get("remote_port"))
        if local_key in port_map:
            new_link["local_port"] = port_map[local_key]
        if remote_key in port_map:
            new_link["remote_port"] = port_map[remote_key]
        resolved.append(new_link)
    return resolved


class FilterModule(object):
    def filters(self):
        return {
            "ew_breakout_server_links": ew_breakout_server_links,
            "ew_fabric_links": ew_fabric_links,
            "resolve_topology_link_ports": resolve_topology_link_ports,
            "expand_switch_port_nics": expand_switch_port_nics,
        }
