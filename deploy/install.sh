#!/usr/bin/env bash

set -x

WORKDIR=/opt/ufo_lab/

UFO_SIMULATOR_DIR=${WORKDIR}/ufo-simulator
UFO_SIMULATOR_ANSIBLE_DIR=${UFO_SIMULATOR_DIR}/ansible
UFO_ARTIFACTS_DIR=${UFO_SIMULATOR_ANSIBLE_DIR}/artifacts
UFO_K8S_ARTIFACTS_DIR=${UFO_ARTIFACTS_DIR}/k8s
export DEBIAN_FRONTEND=noninteractive
export PIP_BREAK_SYSTEM_PACKAGES=1
export KUBECONFIG=/root/.kube/config
export NETRIS_LICENSE=${NETRIS_LICENSE:-''}
export UFO_SIMULATOR_REFSPEC=${UFO_SIMULATOR_REFSPEC:-'main'}
export FABRIC_BACKEND=${FABRIC_BACKEND:-"netris"}
export NODE_TYPE=${NODE_TYPE:-"cmp"}

BASE_INVENTORY=${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml
ANSIBLE_INTENTORY_ARG="-i ${BASE_INVENTORY}"
HOSTNAME=$(hostname -s)

apt update && apt install -y python3-pip

pip3 install ansible

mkdir -p ${WORKDIR}

if [[ ! -d $UFO_SIMULATOR_ANSIBLE_DIR ]]; then
    git clone https://github.com/k0rdent/ufo-simulator $UFO_SIMULATOR_DIR
    pushd $UFO_SIMULATOR_DIR
    git fetch origin ${UFO_SIMULATOR_REFSPEC}:FETCH_HEAD
    git checkout FETCH_HEAD
    popd
fi

CUMULUS_NEW_PASSWORD=$(date +%s | sha256sum | base64 | head -c 15)
NETRIS_ADMIN_PASSWORD=$(date +%s | sha256sum | base64 | head -c 15)
REDFISH_PASSWORD=$(date +%s | sha256sum | base64 | head -c 15)
CTL_PUBLIC_IP=$(ip route get 4.2.2.1 | awk '{print $7}' |tr -d "\n")

sed -i "s/<CUMULUS_NEW_PASSWORD>/${CUMULUS_NEW_PASSWORD}/g" ${UFO_SIMULATOR_ANSIBLE_DIR}/vars/common.yml
sed -i "s/<NETRIS_ADMIN_PASSWORD>/${NETRIS_ADMIN_PASSWORD}/g" ${UFO_SIMULATOR_ANSIBLE_DIR}/vars/common.yml
sed -i "s/<REDFISH_PASSWORD>/${REDFISH_PASSWORD}/g" ${UFO_SIMULATOR_ANSIBLE_DIR}/vars/common.yml
sed -i "s/<CTL_PUBLIC_IP>/${CTL_PUBLIC_IP}/g" ${UFO_SIMULATOR_ANSIBLE_DIR}/vars/common.yml
sed -i "s/<NETRIS_LICENSE>/${NETRIS_LICENSE}/g" ${UFO_SIMULATOR_ANSIBLE_DIR}/vars/common.yml
sed -i "s/sdn_provider:.*$/sdn_provider: ${FABRIC_BACKEND}/g" ${UFO_SIMULATOR_ANSIBLE_DIR}/vars/common.yml

# TODO: fix ugly hack
if [[ ${NODE_TYPE} == "cmp" ]]; then
    ansible-playbook ${ANSIBLE_INTENTORY_ARG} ${UFO_SIMULATOR_ANSIBLE_DIR}/k0s.yml --limit ${HOSTNAME} || /bin/true
    sleep 30
    rm -rf /root/.kube/config
    
    ansible-playbook ${ANSIBLE_INTENTORY_ARG} ${UFO_SIMULATOR_ANSIBLE_DIR}/k0s.yml --limit ${HOSTNAME} || /bin/true
    # Give some time for kubernetes to start
    sleep 120
fi

ansible-playbook ${ANSIBLE_INTENTORY_ARG} ${UFO_SIMULATOR_ANSIBLE_DIR}/setup-network.yml --limit ${HOSTNAME}

if [[ ${NODE_TYPE} == "gtw" ]]; then
    ansible-playbook ${ANSIBLE_INTENTORY_ARG} ${UFO_SIMULATOR_ANSIBLE_DIR}/frr.yml --limit ${HOSTNAME}
fi

if [[ ${NODE_TYPE} == "cmp" ]]; then
    ansible-playbook ${ANSIBLE_INTENTORY_ARG} ${UFO_SIMULATOR_ANSIBLE_DIR}/render-k8s-artifacts.yml --limit ${HOSTNAME}

    ansible-playbook ${ANSIBLE_INTENTORY_ARG} ${UFO_SIMULATOR_ANSIBLE_DIR}/libvirt.yml --limit ${HOSTNAME}
    # Create vms to initialize PXE interface used later in kcm/ironic
    ansible-playbook ${ANSIBLE_INTENTORY_ARG} ${UFO_SIMULATOR_ANSIBLE_DIR}/create-vms.yml --limit ${HOSTNAME}
    ansible-playbook ${ANSIBLE_INTENTORY_ARG} ${UFO_SIMULATOR_ANSIBLE_DIR}/create-switches.yml --limit ${HOSTNAME}
    
    ansible-playbook ${ANSIBLE_INTENTORY_ARG} ${UFO_SIMULATOR_ANSIBLE_DIR}/ipa.yml --limit ${HOSTNAME}
    ansible-playbook ${ANSIBLE_INTENTORY_ARG} ${UFO_SIMULATOR_ANSIBLE_DIR}/kcm.yml --limit ${HOSTNAME}
    ansible-playbook ${ANSIBLE_INTENTORY_ARG} ${UFO_SIMULATOR_ANSIBLE_DIR}/lvp.yml --limit ${HOSTNAME}
    ansible-playbook ${ANSIBLE_INTENTORY_ARG} ${UFO_SIMULATOR_ANSIBLE_DIR}/metallb.yml --limit ${HOSTNAME}
    if [[ ${FABRIC_BACKEND} == "netris" ]]; then
        ansible-playbook ${ANSIBLE_INTENTORY_ARG} ${UFO_SIMULATOR_ANSIBLE_DIR}/netris-controller.yml --limit ${HOSTNAME}
        ansible-playbook ${ANSIBLE_INTENTORY_ARG} ${UFO_SIMULATOR_ANSIBLE_DIR}/netris-operator.yml --limit ${HOSTNAME}
    fi
    
    ansible-playbook ${ANSIBLE_INTENTORY_ARG} ${UFO_SIMULATOR_ANSIBLE_DIR}/ufo.yml --limit ${HOSTNAME}
    
    if [[ ${FABRIC_BACKEND} == "verity" ]]; then
        ansible-playbook ${ANSIBLE_INTENTORY_ARG} ${UFO_SIMULATOR_ANSIBLE_DIR}/verity.yml --limit ${HOSTNAME}
    fi
    
    # Wait everything is ready before moving forwad
    kubectl wait --for=condition=Ready=True management/kcm --timeout=1800s
    kubectl wait --for=condition=ready pod --all --all-namespaces --timeout=1800m
    
    if [[ ${FABRIC_BACKEND} == "netris" ]]; then
        ansible-playbook ${ANSIBLE_INTENTORY_ARG} ${UFO_SIMULATOR_ANSIBLE_DIR}/configure-switches.yml --limit 'all:!gtws'
        ansible-playbook ${ANSIBLE_INTENTORY_ARG} ${UFO_SIMULATOR_ANSIBLE_DIR}/configure-sg.yml --limit 'all:!gtws'
    fi
    
    # Register resources
    if [[ ${FABRIC_BACKEND} == "netris" ]]; then
        kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/site-default.yaml
    fi
    
    kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/static/pxe-net.yaml
    kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/static/subnetpool-default.yaml
    kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/static/vpc-internet.yaml
    
    if [[ ${FABRIC_BACKEND} == "netris" ]]; then
        kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/netris_ipam.yaml
        # Wait ipam to be handled before applying other resources
        sleep 30

        switch_manifests=(
            spine-0.yaml
            spine-1.yaml
            leaf-0.yaml
            leaf-1.yaml
            ext-leaf-0.yaml
            ext-leaf-1.yaml
        )
        for manifest in "${switch_manifests[@]}"; do
            kubectl apply -f "${UFO_K8S_ARTIFACTS_DIR}/${manifest}"
            # Wait switch to be handled before applying other resources
            sleep 5
        done
        kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/ctl.yaml
        kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/sg-0.yaml
        kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/sg-1.yaml
        kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/vm-0.yaml
        kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/vm-1.yaml
        kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/vm-2.yaml

        kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/bgp-external-gateway.yaml
    fi
    
    # Wait netris is fully initialized
    sleep 120
    
    for i in {0..2}; do
        kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/vm-${i}_bmh.yaml
    done
    
    if [[ ${FABRIC_BACKEND} == "netris" ]]; then
        # Wait for bmhs are
        for i in {0..2}; do
            kubectl wait -n kcm-system --for=jsonpath='{.status.provisioning.state}'='available' baremetalhost/vm-${i}  --timeout=1800s
        done
    fi
fi
