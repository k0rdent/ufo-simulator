#!/bin/bash
set -ex

# ensure we don't re-source this in the same environment
[[ -z "$_INSTALL_SCRIPT" ]] || return 0
declare -r -g _INSTALL_SCRIPT=1

export NETRIS_LICENSE=$netris_license
export UFO_SIMULATOR_REFSPEC=$ufo_simulator_refspec
export UFO_SIMULATOR_REFSPEC=${UFO_SIMULATOR_REFSPEC:-"main"}
export NODE_TYPE=$node_type

function wait_condition_send {
    local status=${1:-SUCCESS}
    local reason=${2:-\"empty\"}
    local data=${3:-\"empty\"}
    local data_binary="{\"status\": \"$status\", \"reason\": \"$reason\", \"data\": $data}"
    echo "Trying to send signal to wait condition 5 times: $data_binary"
    WAIT_CONDITION_NOTIFY_EXIT_CODE=2
    i=0
    while (( ${WAIT_CONDITION_NOTIFY_EXIT_CODE} != 0 && ${i} < 5 )); do
        $wait_condition_notify -k --data-binary "$data_binary" && WAIT_CONDITION_NOTIFY_EXIT_CODE=0 || WAIT_CONDITION_NOTIFY_EXIT_CODE=2
        i=$((i + 1))
        sleep 1
    done
    if (( ${WAIT_CONDITION_NOTIFY_EXIT_CODE} !=0 && "${status}" == "SUCCESS" ))
    then
        status="FAILURE"
        reason="Can't reach metadata service to report about SUCCESS."
    fi
    if [ "$status" == "FAILURE" ]; then
        exit 1
    fi
}

# Exit on any errors
function handle_exit {
    if [ $? != 0 ] ; then
        wait_condition_send "FAILURE" "Script terminated with an error."
    fi
}
trap handle_exit EXIT

git clone https://github.com/k0rdent/ufo-simulator /tmp/ufo-simulator
pushd /tmp/ufo-simulator
git fetch origin ${UFO_SIMULATOR_REFSPEC}:FETCH_HEAD
git checkout FETCH_HEAD
popd

function ensure_ip_forward {
    SYSCTL_FILE="/etc/sysctl.d/99-ip-forward.conf"
    
    echo "→ Creating persistent sysctl file → $SYSCTL_FILE"
    
    cat > "$SYSCTL_FILE" << 'EOF'
# Enable IP forwarding (required for NAT/masquerading)
net.ipv4.ip_forward = 1
EOF

    chmod 644 "$SYSCTL_FILE"
    sysctl --system   # apply all pending .d files (including ours)
}

function setup_masquerade {
    export DEBIAN_FRONTEND=noninteractive
    local INTERFACE=$(ip route get 4.2.2.1 | awk '{print $5}' | tr -d '\n')
    
    if ! dpkg -l | grep -q iptables-persistent; then
        apt-get update -qq
        apt-get install -y iptables-persistent
    fi
    
    # Check if the rule already exists
    if ! iptables-save | grep -q -- "-A POSTROUTING  -o $INTERFACE .* -j MASQUERADE"; then
        iptables -t nat -A POSTROUTING -o "$INTERFACE" -s 10.10.0.0/16 -j MASQUERADE
        iptables -t nat -A POSTROUTING -o "$INTERFACE" -s 10.200.0.0/16 -j MASQUERADE
        echo "Rule added."
    else
        echo "Rule already exists — skipping."
    fi
    
    /usr/sbin/netfilter-persistent save
}

if [[ ${NODE_TYPE} == "gtw" ]]; then
    echo "Running node type $NODE_TYPE"
    ensure_ip_forward
    setup_masquerade

fi

wait_condition_send "SUCCESS" "Instance successfuly started."
