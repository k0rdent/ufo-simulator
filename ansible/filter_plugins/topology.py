"""Topology helpers for ufo-simulator ansible playbooks."""


def _port_is_breakout(port, port_prefix):
    """True when port uses breakout naming (e.g. eth1/1/1 or swp1/1)."""
    if not isinstance(port, str):
        return False
    rest = port[len(port_prefix) :] if port_prefix and port.startswith(port_prefix) else port
    return "/" in rest


def ew_breakout_server_links(leafs, nodes, port_prefix="swp", eth_base=5, breakout=2):
    """Build EW leaf→server links with N-way breakout port names.

    Even node indices share odd physical ports; odd indices share even ports.
    Example (2-way)::

        leaf-0 1/1 → node-0 ew nic-0
        leaf-0 1/2 → node-2 ew nic-0
        leaf-0 2/1 → node-1 ew nic-0
        leaf-0 2/2 → node-3 ew nic-0
        leaf-1 1/1 → node-0 ew nic-1
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
                    "local_port": "%s%d/%d" % (port_prefix, phys, lane),
                    "remote": node_name,
                    "remote_port": eth,
                    "role": role,
                }
            )
    return links


def resolve_topology_link_ports(links, switches, port_prefix="swp"):
    """Remap switch-side ports to sequential names matching virtio NIC order.

    Cumulus names data-plane NICs swp1..N in the order QEMU attaches them.
    create-switch-vm attaches NICs as: all links where the switch is *local*
    (topology order), then all links where it is *remote*. Topology YAML may
    declare higher port numbers (e.g. swp27 for spine uplinks) that only match
    when enough earlier links exist. This filter renumbers declared switch
    ports to the actual sequential names so Netris/UFO Link CRs match LLDP.

    Breakout ports (prefix + phys/lane, e.g. eth1/1/1 or swp1/1) are preserved
    as declared; they still consume a NIC slot so later remapped ports stay
    ordered correctly.

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
            "resolve_topology_link_ports": resolve_topology_link_ports,
        }
