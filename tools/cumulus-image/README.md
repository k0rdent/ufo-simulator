# Cumulus Linux VM images from ONIE installers

Two scripts that turn a Cumulus Linux ONIE installer
(`cumulus-linux-<version>-<platform>-amd64.bin`) into a qcow2 for
KVM/libvirt, and make that qcow2 safe to clone.

| Script | Input | Output |
|---|---|---|
| `cumulus-bin-to-qcow2.sh` | installer `.bin` and a Cumulus VX qcow2 | qcow2 with the installer's release installed (booted once) |
| `cumulus-qcow2-generalize.sh` | a booted Cumulus qcow2 | qcow2 that gives every clone its own identity |

Both run as root on a Linux KVM host. They were tested on Ubuntu 22.04
(libvirt 8.0, QEMU 6.2, libguestfs 1.46) with
`cumulus-linux-5.18.0-mlx-amd64.bin` and the Cumulus VX 5.15.0 qcow2.

## Why the installer has to be installed

The `.bin` is not a disk image. It is a shell script with a tar archive
appended: the installer scripts, a kernel, an initrd and the root file system
(`sysroot.tar`). You can look inside without running it:

```bash
sed -n '/^exit_marker$/q;p' cumulus-linux-5.18.0-mlx-amd64.bin | less
sed -e '1,/^exit_marker$/d' cumulus-linux-5.18.0-mlx-amd64.bin | tar tvf -
```

It installs only from ONIE: it repartitions the disk, installs GRUB and then
unpacks the system on the next boot. NVIDIA's Cumulus VX qcow2 has an ONIE
partition (`ONIE-BOOT`, platform `x86_64-cumulus_vx-r0`), so
`cumulus-bin-to-qcow2.sh` boots a copy of a VX image into ONIE install mode and
runs the installer there, the way a switch is installed.

The installer accepts ASIC images (`mlx`, `bcm`) on VX and prints
`Warning: installing non-vx image on vx system`. In the VM, `switchd` starts
with `-vx` (software forwarding); NVUE, FRR and the rest work as on VX.

## Requirements

- A Linux KVM host and root.
- Packages (Ubuntu/Debian):

  ```bash
  apt install qemu-kvm libvirt-daemon-system virtinst libguestfs-tools grub2-common python3 curl openssh-client iproute2
  ```

- A libvirt network with DHCP for the build VM, by default `default`
  (`virbr0`, 192.168.122.0/24). The installer is served over HTTP on the
  network's host address, port 8080 by default; the host firewall must let the
  VM reach it.
- About 40 GB free next to the output file and 8 GB of free RAM for the build VM.
- A Cumulus VX qcow2 that contains ONIE, for example NVIDIA's VX 5.15.0
  image, which the tests used:

  ```bash
  curl -fLO https://networkingdownloads.nvidia.com/custhelp/Non_Monetized_Products/Software/CumulusSoftware/CumulusVX/cumulus-linux-5.15.0-vx-amd64-qemu.qcow2
  ```

- The installer `.bin` on the host. On macOS, copy it from Terminal.app: apps
  without the Files and Folders permission (including Claude) can list
  `~/Downloads` but not read it.

  ```bash
  scp ~/Downloads/cumulus-linux-5.18.0-mlx-amd64.bin root@<kvm-host>:/tmp/
  ```

## 1. Build the qcow2

```bash
sudo ./cumulus-bin-to-qcow2.sh \
    --bin /tmp/cumulus-linux-5.18.0-mlx-amd64.bin \
    --base /var/lib/libvirt/images/cumulus-linux-5.15.0-vx-amd64-qemu.qcow2 \
    --output /var/lib/libvirt/images/cumulus-linux-5.18.0-mlx-amd64-qemu.qcow2
```

`--base` also accepts a URL. The base image is only read; the script works on
a copy. A run takes about 40 minutes, most of it the Cumulus installer and
the final compression. It:

1. Checks the installer's payload checksum and reads its release.
2. Copies the base image into a new disk (`--size`, default 32G) and grows
   the partition table.
3. Sets the same flags `onie-select -i` would: `onie_mode=install` in ONIE's
   grubenv and `next_entry=ONIE` in Cumulus' grubenv.
4. Serves the `.bin` at `http://<network host address>:8080/onie-installer`.
5. Boots a transient VM from the disk. ONIE gets a DHCP lease; the script
   logs in to ONIE over SSH (root, no password), stops ONIE discovery and runs
   `onie-nos-install <url>`.
6. Waits for the Cumulus installer to partition the disk, install and boot
   Cumulus, then lets the first boot finish (`--settle`, default 240 s) and
   shuts the VM down.
7. Checks `/etc/image-release` on the disk and writes a compacted,
   compressed qcow2 plus a `.sha256` file.

The result has been booted once, so it carries that boot's machine-id, SSH
host keys and so on. Generalize it before cloning it.

Run `./cumulus-bin-to-qcow2.sh --help` for all options.

## 2. Generalize it for cloning

```bash
sudo ./cumulus-qcow2-generalize.sh \
    /var/lib/libvirt/images/cumulus-linux-5.18.0-mlx-amd64-qemu.qcow2 \
    /var/lib/libvirt/images/cumulus-linux-5.18.0-mlx-amd64-qemu-generic.qcow2
```

The input is only read and must not be in use by a VM. A run takes about 15
minutes, most of it compression. The script prints a check list at the end
and fails if any check fails.

What it changes, and what recreates it on a clone's first boot:

| State | Change | Recreated by |
|---|---|---|
| `/etc/machine-id` | emptied; `/var/lib/dbus/machine-id` becomes a link to it, both on `CL-VAR` and in each system slot's own `/var` | systemd |
| SSH host keys | deleted | Cumulus first boot (`10_ssh_keygen`) |
| REST API key and certificate (`/etc/ssl/private/cumulus.key`, `/etc/ssl/certs/cumulus.pem`) | deleted | Cumulus first boot (`12_api_cert_gen`) |
| NVUE HTTPS key and certificate, nginx NVUE TLS config | deleted; nginx NVUE config reset from the never-booted system slot | `nvued` when it applies the startup config |
| NVUE database and cache (`/var/lib/nvue`, including the cached system MAC) | deleted | `nvued`, from `/etc/nvue.d/startup.yaml` |
| Cumulus first boot | re-armed: done markers removed, `cumulus-firstboot*.service` enabled | — |
| Initial config backup (in `/var` and in each slot's private `/var`; it contains the old keys) | deleted | Cumulus first boot (`99_backup`) |
| `send dhcp-client-identifier` in `/etc/dhcp/dhclient.conf` | commented out (skip with `--keep-dhcp-client-id`) | dhclient then identifies by MAC |
| Logs, journal, `wtmp`/`btmp`/`lastlog`, `/var/tmp`, `/var/support` | deleted or emptied | — |
| systemd random seed | deleted | systemd |

Why some of these matter:

- The installer writes the install host's SMBIOS serial into `dhclient.conf`
  as the DHCP client identifier. In a VM without a serial that is
  `Not Specified`, so without this change every clone asks for the same lease
  and dnsmasq hands them the same address.
- Cumulus 5.18 ships a fixed machine-id (`38edba16…`) in its root file system,
  so every install has the same one until it is reset. Emptying
  `/etc/machine-id` is not enough: systemd starts before `CL-VAR` is mounted,
  finds the root partition's own `/var/lib/dbus/machine-id` (a regular file
  with the same id) and reuses it.
- An empty machine-id makes systemd apply unit presets on the first boot, and
  Debian's `90-systemd.preset` would enable `systemd-networkd` next to
  ifupdown2. The script adds `/etc/systemd/system-preset/00-generalized-image.preset`
  (`disable *`) and a first-boot hook,
  `/usr/lib/cumulus/firstboot.d/00_generalized_image.firstboot`, that removes
  the preset file and itself.

Re-arming the first boot is what Cumulus' own `factory-reset` does; the
first-boot scripts are written to run again on a configured system.

Not changed: user passwords (`cumulus`/`cumulus`, changed at first login),
ZTP state, the NVUE startup configuration and the never-booted system slot
(apart from its copy of the config backup).

Run `./cumulus-qcow2-generalize.sh --help` for all options.

## Using the generalized image

Use it as a read-only backing file and give each VM its own copy-on-write
disk, as `ansible/create-switches.yml` does with the VX image. Do not boot the
generalized image itself: that uses up its first boot.

```bash
qemu-img create -f qcow2 -F qcow2 \
    -b /var/lib/libvirt/images/cumulus-linux-5.18.0-mlx-amd64-qemu-generic.qcow2 \
    /var/lib/libvirt/images/sw1.qcow2

virt-install --name sw1 --memory 4096 --vcpus 2 --cpu host-passthrough \
    --machine q35 --import --boot hd \
    --disk path=/var/lib/libvirt/images/sw1.qcow2,format=qcow2,bus=sata \
    --network network=default,model=virtio \
    --graphics none --os-variant debian11 --noautoconsole
```

A clone's first boot is much slower than later boots: it generates SSH and
TLS keys (one of them 4096-bit RSA), rebuilds the NVUE database, applies the
full startup configuration and writes the initial config backup. With 15
switches of 50 NICs each starting together on a 32-CPU host, a switch took
about 10 minutes to reach `login:`. Meanwhile the serial console stays empty
after `Loading initial ramdisk`, and pressing Enter does nothing until the
login prompt appears.

Do not destroy a clone during its first boot. The first-boot job disables
itself before it starts, so an interrupted first boot is not retried; delete
that VM's copy-on-write disk and create it again instead.

Log in as `cumulus` / `cumulus`; you are asked to set a new password.

To check a clone:

```bash
cat /etc/image-release                              # IMAGE_RELEASE, IMAGE_SWITCH_ARCHITECTURE
cat /etc/machine-id                                 # different on every clone
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub    # different on every clone
systemctl is-enabled systemd-networkd               # disabled
ls /etc/systemd/system-preset                       # gone after the first boot
```

## Publishing an image for the lab

`ansible/group_vars/all.yml` points the Verity backend at
`cumulus-linux-5.18.0-mlx-amd64-qemu-generic.qcow2` in Artifactory
(`binary-dev-k0rdent-ai-local/ufo/verity/`); Netris stays on Cumulus VX from
NVIDIA. To publish a new generalized build under that name:

```bash
sha=$(cut -d' ' -f1 cumulus-linux-5.18.0-mlx-amd64-qemu-generic.qcow2.sha256)
curl -u <user> -H "X-Checksum-Sha256: $sha" \
    -T cumulus-linux-5.18.0-mlx-amd64-qemu-generic.qcow2 \
    https://artifactory.mcp.mirantis.net/artifactory/binary-dev-k0rdent-ai-local/ufo/verity/cumulus-linux-5.18.0-mlx-amd64-qemu-generic.qcow2
```

`create-switches.yml` downloads the image only when
`/var/lib/libvirt/images/<name>` does not exist, so a host keeps an older file
of the same name. Run `destroy-switches.yml`, delete that file, then run
`create-switches.yml` again.

## Troubleshooting

- **`port 8080 is in use`**: pass another `--port`.
- **`Failed to start hw-management.service`** (and `smond`, `ledmgrd`,
  `hw-management-*` failing on a dependency): expected with an `mlx` image in
  a VM; these services drive NVIDIA switch hardware.
- **`ONIE got no DHCP lease`**: the `--network` needs DHCP. The VM's serial
  log is `console.log` in the work directory, which is kept on failure.
- **ONIE's serial console stops after the kernel messages**: expected. ONIE's
  own console is on `tty0`, which is why the script drives ONIE over SSH.
- **`ONIE could not install`**: the script prints ONIE's log. Usually the VM
  could not reach the HTTP server (host firewall) or the installer is for
  another CPU architecture.
- **`cannot open ...; shut down any VM that uses it`** (generalize): the input
  disk is open in a running VM.
- **Doing it by hand**: set the two grubenv variables from step 3 with
  `guestfish` and `grub-editenv`, boot the disk, SSH to ONIE as root, run
  `onie-stop` and `onie-nos-install http://<host>:<port>/<file>.bin`. ONIE's
  SSH server needs `-o HostKeyAlgorithms=+ssh-rsa -o KexAlgorithms=+diffie-hellman-group14-sha1`.
  If it does not reboot after "NOS install successful", run `/sbin/reboot`
  (plain `reboot` is not on the PATH of an SSH session).
