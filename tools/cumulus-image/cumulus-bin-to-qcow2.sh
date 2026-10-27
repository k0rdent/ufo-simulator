#!/usr/bin/env bash
#
# Build a Cumulus Linux qcow2 for KVM/libvirt from a Cumulus ONIE installer
# (.bin), for example cumulus-linux-5.18.0-mlx-amd64.bin.
#
# The .bin is an installer, not a disk image, so it has to be installed: the
# script boots a copy of a Cumulus VX qcow2 (which ships ONIE) into ONIE
# install mode and feeds it the .bin over HTTP, the way a switch is installed.
#
# Run as root on a libvirt/KVM host. See README.md.

set -euo pipefail

SCRIPT_NAME=$(basename "$0")

BIN=""
BASE=""
OUT=""
SIZE="32G"
NETWORK="default"
PORT="8080"
MEMORY="8192"
VCPUS="4"
OS_VARIANT="debian11"
SETTLE="240"
WORKDIR=""
KEEP_WORKDIR="no"
COMPRESS="yes"
FORCE="no"

usage()
{
	cat <<EOF
Usage: $SCRIPT_NAME --bin <installer.bin> --base <vx.qcow2|URL> --output <out.qcow2> [options]

Install a Cumulus Linux ONIE installer into a VM disk and save it as qcow2.

Required:
  -b, --bin PATH          Cumulus ONIE installer, e.g. cumulus-linux-5.18.0-mlx-amd64.bin
      --base PATH|URL     Cumulus VX qcow2 that contains an ONIE-BOOT partition
                          (for example cumulus-linux-5.15.0-vx-amd64-qemu.qcow2);
                          only read, never modified
  -o, --output PATH       Resulting qcow2

Options:
      --size SIZE         Virtual disk size (default: $SIZE)
      --network NAME      libvirt network with DHCP for the build VM (default: $NETWORK)
      --port PORT         HTTP port on the network's host address (default: $PORT)
      --memory MIB        Build VM memory (default: $MEMORY)
      --vcpus N           Build VM vCPUs (default: $VCPUS)
      --os-variant NAME   virt-install --os-variant (default: $OS_VARIANT)
      --settle SECONDS    Time to let the first boot finish after the login
                          prompt appears (default: $SETTLE)
      --workdir DIR       Work directory (default: a new directory next to --output)
      --keep-workdir      Keep the work directory (disk, logs) afterwards
      --no-compress       Write the output without virt-sparsify compression
      --force             Overwrite --output if it exists
  -h, --help              Show this help
EOF
}

log()
{
	printf '%s %s\n' "$(date +%H:%M:%S)" "$*" >&2
}

die()
{
	log "ERROR: $*"
	exit 1
}

while [ $# -gt 0 ]; do
	case "$1" in
	-b|--bin) BIN="$2"; shift 2 ;;
	--base) BASE="$2"; shift 2 ;;
	-o|--output) OUT="$2"; shift 2 ;;
	--size) SIZE="$2"; shift 2 ;;
	--network) NETWORK="$2"; shift 2 ;;
	--port) PORT="$2"; shift 2 ;;
	--memory) MEMORY="$2"; shift 2 ;;
	--vcpus) VCPUS="$2"; shift 2 ;;
	--os-variant) OS_VARIANT="$2"; shift 2 ;;
	--settle) SETTLE="$2"; shift 2 ;;
	--workdir) WORKDIR="$2"; shift 2 ;;
	--keep-workdir) KEEP_WORKDIR="yes"; shift ;;
	--no-compress) COMPRESS="no"; shift ;;
	--force) FORCE="yes"; shift ;;
	-h|--help) usage; exit 0 ;;
	*) usage >&2; die "unknown argument: $1" ;;
	esac
done

[ -n "$BIN" ] && [ -n "$BASE" ] && [ -n "$OUT" ] || { usage >&2; exit 1; }
[ "$(id -u)" = "0" ] || die "run as root (libvirt, libguestfs and ONIE grubenv edits need it)"
[ -f "$BIN" ] || die "installer not found: $BIN"
if [ -e "$OUT" ] && [ "$FORCE" != "yes" ]; then
	die "$OUT exists; pass --force to overwrite"
fi

for tool in qemu-img virsh virt-install guestfish virt-sparsify grub-editenv python3 ssh curl sha256sum ss; do
	command -v "$tool" > /dev/null || die "missing tool: $tool"
done

BIN=$(realpath "$BIN")
OUT=$(realpath -m "$OUT")
if [ -z "$WORKDIR" ]; then
	WORKDIR=$(mktemp -d "$(dirname "$OUT")/cumulus-build.XXXXXX")
else
	mkdir -p "$WORKDIR"
fi
WORKDIR=$(realpath "$WORKDIR")
DISK="$WORKDIR/disk.qcow2"
CONSOLE="$WORKDIR/console.log"
VM_NAME="cumulus-build-$$"
HTTP_PID=""

cleanup()
{
	local rc=$?
	if virsh domstate "$VM_NAME" > /dev/null 2>&1; then
		virsh destroy "$VM_NAME" > /dev/null 2>&1 || true
	fi
	if [ -n "$HTTP_PID" ]; then
		kill "$HTTP_PID" 2> /dev/null || true
	fi
	if [ "$KEEP_WORKDIR" = "yes" ] || [ $rc -ne 0 ]; then
		log "work directory kept: $WORKDIR"
	else
		rm -rf "$WORKDIR"
	fi
}
trap cleanup EXIT

# ONIE runs an old dropbear: allow its RSA host key and SHA1 key exchange.
onie_ssh()
{
	ssh -o BatchMode=yes -o ConnectTimeout=10 -o LogLevel=ERROR \
		-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
		-o HostKeyAlgorithms=+ssh-rsa -o KexAlgorithms=+diffie-hellman-group14-sha1 \
		"root@$ONIE_IP" "$@"
}

# ---------------------------------------------------------------------------
# 1. Check the installer.
# ---------------------------------------------------------------------------
header=$(sed -n '/^exit_marker$/q;p' "$BIN")
grep -q "Cumulus Self-extracting Binary Image" <<< "$header" ||
	die "$BIN is not a Cumulus ONIE installer"
payload_size=$(sed -n 's/^payload_image_size=\([0-9]*\)$/\1/p' <<< "$header")
payload_sha=$(sed -n 's/^CL_INSTALLER_PAYLOAD_SHA256="\([0-9a-f]*\)"$/\1/p' <<< "$header")
[ -n "$payload_size" ] && [ -n "$payload_sha" ] || die "cannot read payload size/checksum from $BIN"

# The payload is the tar archive after the 'exit_marker' line.
payload()
{
	local -
	set +o pipefail
	sed -e '1,/^exit_marker$/d' "$BIN" | head -c "$payload_size"
}

log "verifying installer payload checksum"
[ "$(payload | sha256sum | cut -d' ' -f1)" = "$payload_sha" ] || die "payload checksum mismatch: $BIN is damaged"
control=$(payload | tar xOf - ./control)
release=$(sed -n 's/^Release: *//p' <<< "$control")
arch=$(sed -n 's/^Switch-Architecture: *//p' <<< "$control")
log "installer: Cumulus Linux $release ($arch)"

# ---------------------------------------------------------------------------
# 2. Prepare the disk: a copy of the VX image, booting into ONIE install mode.
# ---------------------------------------------------------------------------
case "$BASE" in
http://*|https://*)
	log "downloading base image $BASE"
	curl -fL --progress-bar -o "$WORKDIR/base.qcow2" "$BASE"
	BASE="$WORKDIR/base.qcow2"
	;;
esac
[ -f "$BASE" ] || die "base image not found: $BASE"

log "creating $SIZE build disk from $BASE"
qemu-img convert -U -O qcow2 "$BASE" "$DISK"
qemu-img resize -q "$DISK" "$SIZE"
guestfish -a "$DISK" run : part-expand-gpt /dev/sda

onie_dev=$(guestfish --ro -a "$DISK" run : findfs-label ONIE-BOOT 2> /dev/null) ||
	die "base image has no ONIE-BOOT partition; use a Cumulus VX qcow2 that ships ONIE"
nos_root=$(guestfish --ro -a "$DISK" run : inspect-os | sed -n 1p)
[ -n "$nos_root" ] || die "no operating system found in the base image"
guestfish --ro -a "$DISK" run : mount-ro "$nos_root" / : cat /boot/grub/grub.cfg > "$WORKDIR/grub.cfg"
grep -q "^menuentry ONIE" "$WORKDIR/grub.cfg" && grep -q 'next_entry' "$WORKDIR/grub.cfg" ||
	die "base image GRUB has no one-shot ONIE entry"

# Same as 'onie-select -i': ONIE in install mode, chainloaded once by GRUB.
log "setting one-time ONIE install boot ($onie_dev, $nos_root)"
guestfish --ro -a "$DISK" run : mount-ro "$onie_dev" / : download /grub/grubenv "$WORKDIR/onie-grubenv"
guestfish --ro -a "$DISK" run : mount-ro "$nos_root" / : download /boot/grub/grubenv "$WORKDIR/nos-grubenv"
grub-editenv "$WORKDIR/onie-grubenv" set onie_mode=install
grub-editenv "$WORKDIR/nos-grubenv" set next_entry=ONIE
guestfish -a "$DISK" run : mount "$onie_dev" / : upload "$WORKDIR/onie-grubenv" /grub/grubenv
guestfish -a "$DISK" run : mount "$nos_root" / : upload "$WORKDIR/nos-grubenv" /boot/grub/grubenv

# ---------------------------------------------------------------------------
# 3. Serve the installer on the libvirt network's host address.
# ---------------------------------------------------------------------------
virsh net-info "$NETWORK" 2> /dev/null | grep -q "^Active: *yes" || die "libvirt network '$NETWORK' is not active"
HOST_IP=$(virsh net-dumpxml "$NETWORK" | sed -n "s/.*<ip address='\([0-9.]*\)'.*/\1/p" | sed -n 1p)
[ -n "$HOST_IP" ] || die "libvirt network '$NETWORK' has no IPv4 host address"
if ss -ltnH "sport = :$PORT" | grep -q .; then
	die "port $PORT is in use on this host; pick another with --port"
fi
mkdir -p "$WORKDIR/http"
ln -sf "$BIN" "$WORKDIR/http/onie-installer"
python3 -m http.server "$PORT" --bind "$HOST_IP" --directory "$WORKDIR/http" \
	> "$WORKDIR/http.log" 2>&1 < /dev/null &
HTTP_PID=$!
URL="http://$HOST_IP:$PORT/onie-installer"
for _ in $(seq 1 10); do
	curl -sfI -m 3 "$URL" > /dev/null && break
	sleep 1
done
curl -sfI -m 3 "$URL" > /dev/null || die "HTTP server did not come up on $URL"

# ---------------------------------------------------------------------------
# 4. Boot ONIE and run the installer.
# ---------------------------------------------------------------------------
log "starting build VM $VM_NAME (serial log: $CONSOLE)"
virt-install --transient --name "$VM_NAME" --memory "$MEMORY" --vcpus "$VCPUS" \
	--cpu host-passthrough --machine q35 --import --boot hd \
	--disk "path=$DISK,format=qcow2,bus=sata" \
	--network "network=$NETWORK,model=virtio" --graphics none \
	--serial "pty,log.file=$CONSOLE" --os-variant "$OS_VARIANT" \
	--noautoconsole > "$WORKDIR/virt-install.log" 2>&1 ||
	die "virt-install failed, see $WORKDIR/virt-install.log"
MAC=$(virsh domiflist "$VM_NAME" | awk -v net="$NETWORK" '$3 == net { print $5 }')

log "waiting for ONIE to get a DHCP lease ($MAC)"
ONIE_IP=""
for _ in $(seq 1 60); do
	ONIE_IP=$(virsh net-dhcp-leases "$NETWORK" --mac "$MAC" | awk '/ipv4/ { sub("/.*", "", $5); print $5 }' | tail -n 1)
	[ -n "$ONIE_IP" ] && break
	sleep 5
done
[ -n "$ONIE_IP" ] || die "ONIE got no DHCP lease; see $CONSOLE"

log "waiting for ONIE SSH on $ONIE_IP"
for _ in $(seq 1 30); do
	onie_ssh true 2> /dev/null && break
	sleep 5
done
onie_ssh true || die "cannot SSH to ONIE at $ONIE_IP"
log "ONIE platform: $(onie_ssh onie-sysinfo -p), ASIC: $(onie_ssh onie-sysinfo -S)"

# ONIE's own discovery never finds the installer (it only tries port 80 and
# TFTP), so stop it and point it at the URL. ONIE reboots itself on success,
# which needs /sbin on PATH.
log "installing $URL from ONIE"
onie_ssh "onie-stop > /dev/null 2>&1 || onie-discovery-stop > /dev/null 2>&1;
	export PATH=/sbin:/usr/sbin:/bin:/usr/bin;
	nohup onie-nos-install '$URL' > /tmp/nos-install.log 2>&1 < /dev/null &"

rebooted()
{
	grep -q "Restarting system" "$CONSOLE" 2> /dev/null
}

onie_log=""
stage1="running"
for _ in $(seq 1 180); do
	sleep 10
	if rebooted; then
		stage1="rebooted"
		break
	fi
	onie_log=$(onie_ssh "cat /var/log/onie.log" 2> /dev/null) || continue
	if grep -q "NOS install successful" <<< "$onie_log"; then
		stage1="done"
		break
	fi
	if grep -q "Failure: Unable to install image" <<< "$onie_log"; then
		grep -E "os-install|onie-exec" <<< "$onie_log" | tail -n 20 >&2
		die "ONIE could not install $URL"
	fi
done
[ "$stage1" != "running" ] || die "ONIE install did not finish in 30 minutes"
grep "os-install:" <<< "$onie_log" | sed 's/^.*os-install: /  /' >&2 || true
if [ "$stage1" = "done" ]; then
	# ONIE reboots on its own; only nudge it if it is still ONIE after a minute.
	for _ in $(seq 1 12); do
		rebooted && break
		sleep 5
	done
	if ! rebooted; then
		log "ONIE did not reboot by itself; rebooting it"
		onie_ssh "sync; /sbin/reboot" > /dev/null 2>&1 || true
	fi
fi
log "stage 1 done; the Cumulus installer now partitions the disk and installs"

# ---------------------------------------------------------------------------
# 5. Wait for the Cumulus installer and the first boot.
# ---------------------------------------------------------------------------
for _ in $(seq 1 270); do
	grep -q "login:" "$CONSOLE" 2> /dev/null && break
	sleep 10
done
grep -q "login:" "$CONSOLE" || die "Cumulus did not reach a login prompt in 45 minutes; see $CONSOLE"
log "Cumulus booted; letting the first boot finish for ${SETTLE}s"
sleep "$SETTLE"

log "shutting down the build VM"
virsh shutdown "$VM_NAME" > /dev/null
for _ in $(seq 1 90); do
	virsh domstate "$VM_NAME" > /dev/null 2>&1 || break
	sleep 2
done
if virsh domstate "$VM_NAME" > /dev/null 2>&1; then
	log "WARNING: VM did not shut down cleanly; powering it off"
	virsh destroy "$VM_NAME" > /dev/null
fi

# ---------------------------------------------------------------------------
# 6. Check and write the result.
# ---------------------------------------------------------------------------
installed=""
for root in $(guestfish --ro -a "$DISK" run : inspect-os); do
	r=$(guestfish --ro -a "$DISK" run : mount-ro "$root" / : cat /etc/image-release 2> /dev/null |
		sed -n 's/^IMAGE_RELEASE=//p') || true
	log "  $root: Cumulus Linux ${r:-?}"
	installed="${installed:-$r}"
done
[ "$installed" = "$release" ] || die "disk does not contain Cumulus Linux $release"

rm -f "$OUT" "$OUT.sha256"
if [ "$COMPRESS" = "yes" ]; then
	log "compacting and compressing into $OUT"
	TMPDIR="$WORKDIR" virt-sparsify --quiet --compress --convert qcow2 -o compat=1.1 "$DISK" "$OUT"
else
	log "writing $OUT"
	qemu-img convert -O qcow2 "$DISK" "$OUT"
fi
(cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")
log "done: $OUT ($(du -h "$OUT" | cut -f1)), Cumulus Linux $release $arch"
log "the image has been booted once; run cumulus-qcow2-generalize.sh before cloning it"
