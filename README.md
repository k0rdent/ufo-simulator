# Ansible Virtual Switch Lab

Creates a virtual Cumulus Linux switch lab using libvirt/KVM with UDP tunnels for inter-switch links.

## Quick Start

```bash
# 1. Create the lab (VMs + links)
ansible-playbook -i inventory.yml create-vms.yml

# 2. Create lab Switches
ansible-playbook -i inventory.yml create-switches.yml

# 3. Configure switches
ansible-playbook -i inventory.yml configure-switches.yml 

# Default credentials: cumulus / NewNet0ps!

# 4. Destroy the lab
ansible-playbook -i inventory.yml destroy-vms.yml
ansible-playbook -i inventory.yml destroy-switches.yml

# 5. Create netris resources
kubectl apply -f artifacts/k8s/
```

## Customizing the Topology

![Network topology diagram](images/netris-topology.png)

Edit `group_vars/all.yml` to modify:

## How It Works

### UDP Tunnels for Switch Links

Each link between switches uses a pair of UDP ports:

```
spine-0:swp1 <--UDP--> leaf-0:swp31

  spine-0 VM                    leaf-0 VM
  ┌─────────────┐              ┌─────────────┐
  │  swp1 NIC   │──────────────│  swp31 NIC  │
  │ local:10000 │   UDP/IP     │ local:10001 │
  │remote:10001 │<────────────>│remote:10000 │
  └─────────────┘              └─────────────┘
```

This is handled by QEMU's `-netdev socket,udp=...` option.

### Management Access

Each VM gets a management NIC using QEMU user-mode networking with SSH port forwarding:

- spine-0: localhost:2200 → VM:22
- leaf-0:  localhost:2201 → VM:22
- etc.

## Useful Commands

```bash
# List running VMs
virsh list

# Console access (escape: Ctrl+])
virsh console leaf-0

# Check switch interfaces
ssh -p 2201 cumulus@127.0.0.1 "nv show interface"

# Check LLDP neighbors
ssh -p 2201 cumulus@127.0.0.1 "nv show service lldp neighbor"

# Check BGP status
ssh -p 2201 cumulus@127.0.0.1 "nv show router bgp neighbor"
```

## Deploy All in one appliance


### Create heat stack AIO
```
cd deploy/heat
openstack stack create -t top.yaml -e env/k0s-aio.yaml ufo-aio-01
openstack stack output show ufo-aio-01 --all
```

### Install simulation

```
cd /opt/ufo_lab/netris-simulator
bash deploy/install.sh
```

### Run playbooks
Check install.sh and run playbooks sequentially
