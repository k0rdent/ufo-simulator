"""Topology helpers for ufo-simulator ansible playbooks."""


def resolve_topology_link_ports(links, switches, port_prefix="swp"):
    """Remap switch-side ports to sequential names matching virtio NIC order.

    Cumulus names data-plane NICs swp1..N in the order QEMU attaches them.
    create-switch-vm attaches NICs as: all links where the switch is *local*
    (topology order), then all links where it is *remote*. Topology YAML may
    declare higher port numbers (e.g. swp27 for spine uplinks) that only match
    when enough earlier links exist. This filter renumbers declared switch
    ports to the actual sequential names so Netris/UFO Link CRs match LLDP.

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
                    port_map[key] = "%s%s" % (port_prefix, n)
                    n += 1
        for link in links:
            if link.get("remote") == sw:
                declared = link["remote_port"]
                key = (sw, declared)
                if key not in port_map:
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
            "resolve_topology_link_ports": resolve_topology_link_ports,
        }
