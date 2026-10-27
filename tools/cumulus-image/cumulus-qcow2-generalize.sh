#!/usr/bin/env bash
#
# Generalize a booted Cumulus Linux qcow2 (for example the output of
# cumulus-bin-to-qcow2.sh) so that every VM cloned from it gets its own
# identity: machine-id, SSH host keys, TLS keys, DHCP client identifier, NVUE
# state and logs. Where Cumulus' own /usr/bin/factory-reset has a step for
# something, the same step is done here, offline.
#
# Run as root on a host with libguestfs. See README.md.

set -euo pipefail

SCRIPT_NAME=$(basename "$0")

IN=""
OUT=""
WORKDIR=""
KEEP_WORKDIR="no"
COMPRESS="yes"
KEEP_CLIENT_ID="no"
FORCE="no"

usage()
{
	cat <<EOF
Usage: $SCRIPT_NAME [options] <input.qcow2> <output.qcow2>

Write a generalized copy of a Cumulus Linux VM disk. The input is only read;
it must not be in use by a running VM.

Options:
      --keep-dhcp-client-id  Keep the 'send dhcp-client-identifier' line the
                             installer wrote into /etc/dhcp/dhclient.conf
      --workdir DIR          Work directory (default: a new directory next to the output)
      --keep-workdir         Keep the work directory afterwards
      --no-compress          Write the output without virt-sparsify compression
      --force                Overwrite the output if it exists
  -h, --help                 Show this help
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
	--keep-dhcp-client-id) KEEP_CLIENT_ID="yes"; shift ;;
	--workdir) WORKDIR="$2"; shift 2 ;;
	--keep-workdir) KEEP_WORKDIR="yes"; shift ;;
	--no-compress) COMPRESS="no"; shift ;;
	--force) FORCE="yes"; shift ;;
	-h|--help) usage; exit 0 ;;
	-*) usage >&2; die "unknown option: $1" ;;
	*)
		if [ -z "$IN" ]; then IN="$1"; elif [ -z "$OUT" ]; then OUT="$1"; else
			usage >&2; die "unexpected argument: $1"
		fi
		shift
		;;
	esac
done

[ -n "$IN" ] && [ -n "$OUT" ] || { usage >&2; exit 1; }
[ "$(id -u)" = "0" ] || die "run as root (libguestfs needs it to change files owned by other users)"
[ -f "$IN" ] || die "input not found: $IN"
if [ -e "$OUT" ] && [ "$FORCE" != "yes" ]; then
	die "$OUT exists; pass --force to overwrite"
fi
for tool in qemu-img guestfish guestmount guestunmount virt-filesystems virt-sparsify sha256sum; do
	command -v "$tool" > /dev/null || die "missing tool: $tool"
done
qemu-img info "$IN" > /dev/null 2>&1 || die "cannot open $IN; shut down any VM that uses it"

IN=$(realpath "$IN")
OUT=$(realpath -m "$OUT")
if [ -z "$WORKDIR" ]; then
	WORKDIR=$(mktemp -d "$(dirname "$OUT")/cumulus-generalize.XXXXXX")
else
	mkdir -p "$WORKDIR"
fi
WORKDIR=$(realpath "$WORKDIR")
WORK="$WORKDIR/disk.qcow2"
MNT="$WORKDIR/mnt"
PIDFILE="$WORKDIR/guestmount.pid"
mkdir -p "$MNT"

cleanup()
{
	local rc=$?
	mountpoint -q "$MNT" 2> /dev/null && guestunmount "$MNT" > /dev/null 2>&1 || true
	if [ "$KEEP_WORKDIR" = "yes" ] || [ $rc -ne 0 ]; then
		log "work directory kept: $WORKDIR"
	else
		rm -rf "$WORKDIR"
	fi
}
trap cleanup EXIT

# guestmount returns before its appliance has released the disk; wait for it
# so the next libguestfs call can open the image.
gmount()
{
	guestmount --pid-file "$PIDFILE" -a "$WORK" "$@" "$MNT"
}

gunmount()
{
	local pid
	pid=$(cat "$PIDFILE")
	guestunmount "$MNT"
	while kill -0 "$pid" 2> /dev/null; do
		sleep 1
	done
}

# ---------------------------------------------------------------------------
# 1. Copy the input and find the disk layout.
# ---------------------------------------------------------------------------
log "copying $IN"
qemu-img convert -O qcow2 "$IN" "$WORK"

filesystems=$(virt-filesystems -a "$WORK" --filesystems --long)
var_dev=$(awk '$4 == "CL-VAR" { print $1 }' <<< "$filesystems")
system_devs=$(awk '$4 ~ /^CL-SYSTEM/ { print $1 }' <<< "$filesystems")
[ -n "$system_devs" ] || system_devs=$(guestfish --ro -a "$WORK" run : inspect-os)
[ -n "$system_devs" ] || die "no Cumulus system partition found in $IN"

# The active slot is the one every slot's fstab mounts as /.
first_dev=$(sed -n 1p <<< "$system_devs")
root_spec=$(guestfish --ro -a "$WORK" run : mount-ro "$first_dev" / : cat /etc/fstab |
	awk '$1 !~ /^#/ && $2 == "/" { print $1 }')
case "$root_spec" in
UUID=*) active_dev=$(guestfish --ro -a "$WORK" run : findfs-uuid "${root_spec#UUID=}") ;;
LABEL=*) active_dev=$(guestfish --ro -a "$WORK" run : findfs-label "${root_spec#LABEL=}") ;;
*) active_dev="$first_dev" ;;
esac

# A slot that never booted is the first-boot baseline for files NVUE rewrites.
fresh_dev=""
for dev in $system_devs; do
	[ "$dev" = "$active_dev" ] && continue
	if [ "$(guestfish --ro -a "$WORK" run : mount-ro "$dev" / : exists /etc/cumulus-firstboot-done)" = "false" ]; then
		fresh_dev="$dev"
		break
	fi
done

active_check=$(guestfish --ro -a "$WORK" run : mount-ro "$active_dev" / : \
	exists /usr/lib/cumulus/firstboot : exists /etc/cumulus-firstboot-done | tr '\n' ' ')
case "$active_check" in
"true true "*) ;;
"true false "*) log "WARNING: $active_dev has not been booted yet; generalizing anyway" ;;
*) die "$active_dev is not a Cumulus Linux root file system" ;;
esac
log "layout: root $active_dev, /var ${var_dev:-on root}, never-booted slot ${fresh_dev:-none}"

mount_args=(-m "$active_dev")
[ -n "$var_dev" ] && mount_args+=(-m "$var_dev:/var")

if [ -n "$fresh_dev" ]; then
	gmount --ro -m "$fresh_dev"
	mkdir -p "$WORKDIR/baseline"
	cp -p "$MNT/etc/nginx/nvue_runtime.conf" "$MNT/etc/nginx/sites-available/nvue.conf" "$WORKDIR/baseline/"
	gunmount
fi

# ---------------------------------------------------------------------------
# 2. Reset the per-machine state.
# ---------------------------------------------------------------------------
gmount "${mount_args[@]}"
R="$MNT"

log "machine-id"
old_id=$(cat "$R/etc/machine-id" 2> /dev/null || true)
: > "$R/etc/machine-id"
if [ -d "$R/var/lib/dbus" ]; then
	rm -f "$R/var/lib/dbus/machine-id"
	ln -s /etc/machine-id "$R/var/lib/dbus/machine-id"
fi

# An empty machine-id makes systemd apply unit presets on first boot, and the
# stock 90-systemd.preset would enable systemd-networkd next to ifupdown2.
# Block that for the first boot only: a first-boot hook removes the guard.
log "first-boot preset guard"
mkdir -p "$R/etc/systemd/system-preset"
cat > "$R/etc/systemd/system-preset/00-generalized-image.preset" <<'EOF'
# Generalized image: keep systemd's first-boot preset-all from enabling
# units (e.g. systemd-networkd).  Removed on first boot by
# /usr/lib/cumulus/firstboot.d/00_generalized_image.firstboot.
disable *
EOF
cat > "$R/usr/lib/cumulus/firstboot.d/00_generalized_image.firstboot" <<'EOF'
#!/bin/sh

# Generalized image: drop the first-boot preset guard, then this script.
rm -f /etc/systemd/system-preset/00-generalized-image.preset
rmdir /etc/systemd/system-preset 2>/dev/null
rm -f "$0"
EOF
chmod 0755 "$R/usr/lib/cumulus/firstboot.d/00_generalized_image.firstboot"

log "SSH host keys"
rm -f "$R"/etc/ssh/ssh_host_*

log "TLS keys and certificates"
rm -f "$R/etc/ssl/private/cumulus.key" "$R/etc/ssl/certs/cumulus.pem"
rm -f "$R/etc/nvue/certificates/private/.https_server.key" \
	"$R/etc/nvue/certificates/public/.https_server.crt"
rm -f "$R/etc/nginx/nvue_tls.conf" "$R/etc/nginx/nvue_mtls.conf"
if [ -d "$WORKDIR/baseline" ]; then
	cp -p "$WORKDIR/baseline/nvue_runtime.conf" "$R/etc/nginx/nvue_runtime.conf"
	cp -p "$WORKDIR/baseline/nvue.conf" "$R/etc/nginx/sites-available/nvue.conf"
else
	log "WARNING: no never-booted slot; NVUE nginx config is left for nvued to regenerate"
fi

# Same as factory-reset: the first-boot scripts regenerate the SSH host keys,
# the REST API certificate and the initial config backup.
log "re-arm Cumulus first boot"
rm -f "$R/etc/cumulus-firstboot-done" "$R/etc/cumulus-firstboot-after-networking-done"
for unit in cumulus-firstboot.service cumulus-firstboot-after-networking.service; do
	[ -e "$R/lib/systemd/system/$unit" ] &&
		ln -sfn "/lib/systemd/system/$unit" "$R/etc/systemd/system/sysinit.target.wants/$unit"
done

log "DHCP client identity and leases"
if [ "$KEEP_CLIENT_ID" != "yes" ]; then
	# The installer pins the client identifier to the install host's SMBIOS
	# serial; clones would all ask for the same lease.
	sed -i 's/^send dhcp-client-identifier /#send dhcp-client-identifier /' "$R/etc/dhcp/dhclient.conf"
fi
rm -f "$R"/var/lib/dhcp/dhclient*.lease "$R"/var/lib/dhcp/dhclient*.leases

log "NVUE state"
rm -rf "$R"/var/lib/nvue/* "$R/root/.local/share/nvue"

log "config backups"
rm -rf "$R/var/lib/config-backup/backups" "$R/var/lib/config-backup/auto-snapshots"

log "logs"
find "$R/var/log" -type f ! -name wtmp ! -name btmp ! -name lastlog -delete
for f in wtmp btmp lastlog; do
	if [ -e "$R/var/log/$f" ]; then
		: > "$R/var/log/$f"
	fi
done
if [ -n "$old_id" ]; then
	rm -rf "$R/var/log/journal/$old_id"
fi
rm -rf "$R"/var/tmp/*
if [ -d "$R/var/support" ]; then
	rm -rf "$R"/var/support/*
	mkdir -p "$R/var/support/core"
fi
if [ -d "$R/var/reset-reason" ]; then
	find "$R/var/reset-reason" \( -type f -o -type l \) -delete
fi

log "random seed"
rm -f "$R/var/lib/systemd/random-seed"

gunmount

# Each system slot has its own /var under the CL-VAR mount point. systemd
# reads /var/lib/dbus/machine-id from there before CL-VAR is mounted and, if
# it is a regular file, reuses that id instead of generating one; a symlink
# is skipped. The slots also keep a copy of the first-boot config backup,
# which holds the old keys.
log "machine-id and config backup copies in the system slots"
for dev in $system_devs; do
	guestfish -a "$WORK" <<EOF
run
mount $dev /
rm-rf /var/lib/config-backup/backups
-rm-f /var/lib/dbus/machine-id
-ln-s /etc/machine-id /var/lib/dbus/machine-id
EOF
done

# ---------------------------------------------------------------------------
# 3. Check the result.
# ---------------------------------------------------------------------------
gmount --ro "${mount_args[@]}"
failed=0
expect()
{
	local what="$1" want="$2" got="$3"
	if [ "$got" = "$want" ]; then
		printf '  ok    %-40s %s\n' "$what" "$got" >&2
	else
		printf '  FAIL  %-40s got %s, want %s\n' "$what" "$got" "$want" >&2
		failed=1
	fi
}
expect "machine-id size" 0 "$(stat -c %s "$R/etc/machine-id")"
expect "SSH host keys" 0 "$(find "$R/etc/ssh" -name 'ssh_host_*' | wc -l)"
expect "REST API key and cert" 0 "$(find "$R/etc/ssl/private/cumulus.key" "$R/etc/ssl/certs/cumulus.pem" 2> /dev/null | wc -l)"
expect "NVUE HTTPS key and cert" 0 "$(find "$R/etc/nvue/certificates" -name '.https_server.*' | wc -l)"
expect "first-boot done markers" 0 "$(find "$R/etc" -maxdepth 1 -name 'cumulus-firstboot*-done' | wc -l)"
expect "first-boot services enabled" 2 "$(find "$R/etc/systemd/system/sysinit.target.wants" -name 'cumulus-firstboot*' | wc -l)"
expect "first-boot preset guard" "disable *" "$(grep -v '^#' "$R/etc/systemd/system-preset/00-generalized-image.preset")"
if [ "$KEEP_CLIENT_ID" != "yes" ]; then
	expect "active dhcp-client-identifier lines" 0 "$(grep -c '^send dhcp-client-identifier' "$R/etc/dhcp/dhclient.conf" || true)"
fi
expect "NVUE database files" 0 "$(find "$R/var/lib/nvue" -name '*.db' | wc -l)"
expect "config backups" 0 "$(find "$R/var/lib/config-backup" -name 'config_backup-*' 2> /dev/null | wc -l)"
expect "random seed" 0 "$(find "$R/var/lib/systemd" -maxdepth 1 -name random-seed | wc -l)"
expect "non-empty files under /var/log" 0 "$(find "$R/var/log" -type f -size +0 | wc -l)"
gunmount
for dev in $system_devs; do
	gmount --ro -m "$dev"
	expect "$dev /var/lib/dbus/machine-id is not a file" 0 \
		"$(find "$R/var/lib/dbus" -maxdepth 1 -name machine-id -type f 2> /dev/null | wc -l)"
	gunmount
done
[ $failed -eq 0 ] || die "generalization checks failed; work disk kept in $WORKDIR"

# ---------------------------------------------------------------------------
# 4. Write the output.
# ---------------------------------------------------------------------------
rm -f "$OUT" "$OUT.sha256"
if [ "$COMPRESS" = "yes" ]; then
	log "compacting and compressing into $OUT"
	TMPDIR="$WORKDIR" virt-sparsify --quiet --compress --convert qcow2 -o compat=1.1 "$WORK" "$OUT"
else
	log "writing $OUT"
	qemu-img convert -O qcow2 "$WORK" "$OUT"
fi
(cd "$(dirname "$OUT")" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")
log "done: $OUT ($(du -h "$OUT" | cut -f1))"
log "use it as a read-only base (qemu-img create -b ...); booting it directly uses up its first boot"
