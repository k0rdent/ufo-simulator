#!/bin/bash
set -ex

# ensure we don't re-source this in the same environment
[[ -z "$_INSTALL_SCRIPT" ]] || return 0
declare -r -g _INSTALL_SCRIPT=1

export NETRIS_LICENSE=$netris_license
export UFO_SIMULATOR_REFSPEC=$ufo_simulator_refspec
export UFO_SIMULATOR_REFSPEC=${UFO_SIMULATOR_REFSPEC:-"main"}

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
bash /tmp/ufo-simulator/deploy/install.sh

rm -rf /tmp/ufo-simulator

wait_condition_send "SUCCESS" "Instance successfuly started."
