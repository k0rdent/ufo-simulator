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


def _mac_from_offset(base_mac, offset):
    """Format ``vm_base_mac`` + 16-bit offset as ``aa:bb:cc:dd:ee:ff``."""
    return "%s:%02x:%02x" % (base_mac, (offset // 256) % 256, offset % 256)


def _switch_mgmt_mac(switch_name, ns_names, ew_names, switch_base_mac, ew_switch_base_mac):
    if switch_name in ew_names:
        return "%s:%02x:00" % (ew_switch_base_mac, ew_names.index(switch_name))
    if switch_name in ns_names:
        return "%s:%02x:00" % (switch_base_mac, ns_names.index(switch_name))
    return "%s:00:00" % switch_base_mac


def _eth_pci_slot(port_name):
    """Stable synthetic PCI slot for byslot matching (not libvirt-assigned)."""
    m = re.match(r"^eth(\d+)$", str(port_name or ""))
    if not m:
        return "0000:a3:00.0"
    n = int(m.group(1))
    if 1 <= n <= 4:
        return "0000:a3:00.%d" % (n - 1)
    if n >= 5:
        return "0000:b%x:00.0" % (n - 5)
    return "0000:a3:00.0"


def _eth_pci_path(port_name, slot):
    if str(port_name) == "eth0" or slot.startswith("0000:01:"):
        return "/devices/pci0000:00/0000:00:01.3/%s/net/%s" % (slot, port_name)
    if slot.startswith("0000:a3:"):
        return "/devices/pci0000:a0/0000:a0:01.3/%s/net/%s" % (slot, port_name)
    bus = slot[5:7]
    return "/devices/pci0000:%s/0000:%s:01.0/%s/net/%s" % (bus, bus, slot, port_name)


def _vm_links_for_server(server_name, all_links):
    """Mirror create-vm.yml: walk all_topology_links in order, keep endpoints for this VM."""
    out = []
    for link in all_links or []:
        if link.get("remote") == server_name:
            out.append(
                {
                    "port": link["remote_port"],
                    "switch": link["local"],
                    "switch_port": link["local_port"],
                }
            )
        elif link.get("local") == server_name:
            out.append(
                {
                    "port": link["local_port"],
                    "switch": link["remote"],
                    "switch_port": link["remote_port"],
                }
            )
    return out


def nico_core_mock_machines(
    nodes,
    all_links,
    vm_base_mac="52:54:00:12",
    vm_port_count=12,
    switch_base_mac="00:01:00:00",
    ew_switch_base_mac="00:01:00:01",
    ns_switches=None,
    ew_switches=None,
    segment_id="00000000-0000-4000-9000-000000000000",
):
    """Build nico-core-mock ``inventory.machines`` from lab topology rules.

    Must stay aligned with create-vms.yml / create-vm.yml:

    - UUID ``00000000-0000-4000-8000-`` + zero-padded decimal index (12 digits)
    - MAC ``vm_base_mac`` + ``vm_index * vm_port_count + nic_index`` (link order)
    - LLDP peer from the matching topology link (switch name + port)
    - Switch chassis MAC from NS/EW switch list order (same as dnsmasq/build-switch-nics)

    Calculable on gtw01 — does not query libvirt on cmp01.
    """
    ns_names = [s["name"] if isinstance(s, dict) else s for s in (ns_switches or [])]
    ew_names = [s["name"] if isinstance(s, dict) else s for s in (ew_switches or [])]
    port_count = int(vm_port_count or 12)
    machines = []

    for vm_index, node in enumerate(nodes or []):
        name = node["name"] if isinstance(node, dict) else node
        base_offset = vm_index * port_count
        machine_id = "00000000-0000-4000-8000-%012d" % vm_index
        vm_links = _vm_links_for_server(name, all_links)

        nics = []
        for nic_index, link in enumerate(vm_links):
            port = link["port"]
            switch = link["switch"]
            switch_port = link["switch_port"]
            mac = _mac_from_offset(vm_base_mac, base_offset + nic_index)
            slot = _eth_pci_slot(port)
            chassis = _switch_mgmt_mac(
                switch, ns_names, ew_names, switch_base_mac, ew_switch_base_mac
            )
            nics.append(
                {
                    "macAddress": mac,
                    "lldp": {
                        "portId": "ifname=%s" % switch_port,
                        "switchId": "mac=%s" % chassis,
                        "switchSystemName": switch,
                    },
                    "pciProperties": {
                        "description": "I350 Gigabit Network Connection",
                        "device": "I350 Gigabit Network Connection",
                        "path": _eth_pci_path(port, slot),
                        "slot": slot,
                        "vendor": "Intel Corporation",
                    },
                }
            )

        primary_mac = (
            nics[0]["macAddress"]
            if nics
            else _mac_from_offset(vm_base_mac, base_offset)
        )
        machines.append(
            {
                "id": machine_id,
                "state": "Ready",
                "interfaces": [
                    {
                        "hostname": "%s.lab.local" % name,
                        "mac_address": primary_mac.lower(),
                        "addresses": ["10.10.0.%d" % (10 + vm_index)],
                        "segment_id": segment_id,
                        "primary_interface": True,
                    }
                ],
                "discovery_info": {
                    "cpuInfo": [
                        {
                            "cores": 16,
                            "model": "AMD EPYC 9115 16-Core Processor",
                            "sockets": 1,
                            "threads": 16,
                            "vendor": "AuthenticAMD",
                        }
                    ],
                    "dmiData": {
                        "biosDate": "04/01/2026",
                        "biosVersion": "R23_F20",
                        "boardName": "MZG3-GU0-%03d" % vm_index,
                        "boardSerial": "PK1N6300%04d" % vm_index,
                        # Must include a non-digit so YAML never coerces to int
                        # (03000308 → 3000308 int breaks proto string boardVersion).
                        "boardVersion": "R23-%02d" % vm_index,
                        "chassisSerial": "2451R26302R1.0U1%03d" % vm_index,
                        "productName": "R263-ZG0-AAL2-%03d" % vm_index,
                        "productSerial": "DPG5NS621A%04d" % vm_index,
                        "sysVendor": "Giga Computing",
                    },
                    "machineArch": "X86_64",
                    "machineType": "x86_64",
                    "networkInterfaces": nics,
                },
                "machine_capabilities": [
                    {
                        "type": "CPU",
                        "name": "AMD EPYC 9115 16-Core Processor",
                        "vendor": "AuthenticAMD",
                        "count": 1,
                    },
                    {
                        "type": "Network",
                        "name": "I350 Gigabit Network Connection",
                        "vendor": "Intel Corporation",
                        "count": len(nics),
                    },
                    {"type": "Memory", "name": "DDR5", "count": 1, "capacity": "262144 MB"},
                ],
            }
        )
    return machines


class FilterModule(object):
    def filters(self):
        return {
            "ew_breakout_server_links": ew_breakout_server_links,
            "ew_fabric_links": ew_fabric_links,
            "resolve_topology_link_ports": resolve_topology_link_ports,
            "expand_switch_port_nics": expand_switch_port_nics,
            "nico_core_mock_machines": nico_core_mock_machines,
        }
