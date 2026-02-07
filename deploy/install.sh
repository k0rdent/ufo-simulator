WORKDIR=/opt/ufo_lab/

UFO_SIMULATOR_DIR=${WORKDIR}/ufo-simulator/
UFO_SIMULATOR_ANSIBLE_DIR=${WORKDIR}/ansible
UFO_ARTIFACTS_DIR=${UFO_SIMULATOR_ANSIBLE_DIR}/artifacts
UFO_K8S_ARTIFACTS_DIR=${UFO_ARTIFACTS_DIR}/k8s
export DEBIAN_FRONTEND=noninteractive
export PIP_BREAK_SYSTEM_PACKAGES=1
export KUBECONFIG=/root/.kube/config
export NETRIS_LICENSE=${NETRIS_LICENSE:-''}

apt update && apt install -y python3-pip

pip3 install ansible

mkdir -p ${WORKDIR}

if [[ ! -d $UFO_SIMULATOR_ANSIBLE_DIR ]]; then
    git clone https://github.com/k0rdent/ufo-simulator $UFO_SIMULATOR_ANSIBLE_DIR
    pushd $UFO_SIMULATOR_ANSIBLE_DIR
    git checkout ${UFO_SIMULATOR_REF}
    popd
fi

CUMULUS_NEW_PASSWORD=$(date +%s | sha256sum | base64 | head -c 15)
NETRIS_ADMIN_PASSWORD=$(date +%s | sha256sum | base64 | head -c 15)
REDFISH_PASSWORD=$(date +%s | sha256sum | base64 | head -c 15)
CTL_PUBLIC_IP=$(ip route get 4.2.2.1 | awk '{print $7}' |tr -d "\n")

sed -i "s/<CUMULUS_NEW_PASSWORD>/${CUMULUS_NEW_PASSWORD}/g" ${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml
sed -i "s/<NETRIS_ADMIN_PASSWORD>/${NETRIS_ADMIN_PASSWORD}/g" ${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml
sed -i "s/<REDFISH_PASSWORD>/${REDFISH_PASSWORD}/g" ${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml
sed -i "s/<CTL_PUBLIC_IP>/${CTL_PUBLIC_IP}/g" ${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml
sed -i "s/<NETRIS_LICENSE>/${NETRIS_LICENSE}/g" ${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml


# TODO: fix ugly hack
ansible-playbook -i ${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml ${UFO_SIMULATOR_ANSIBLE_DIR}/k0s.yml || /bin/true
sleep 30
rm -rf /root/.kube/config

ansible-playbook -i ${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml ${UFO_SIMULATOR_ANSIBLE_DIR}/k0s.yml || /bin/true
# Give some time for kubernetes to start
sleep 120

ansible-playbook -i ${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml ${UFO_SIMULATOR_ANSIBLE_DIR}/libvirt.yml
# Create vms to initialize PXE interface used later in kcm/ironic
ansible-playbook -i ${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml ${UFO_SIMULATOR_ANSIBLE_DIR}/create-vms.yml
ansible-playbook -i ${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml ${UFO_SIMULATOR_ANSIBLE_DIR}/create-switches.yml

ansible-playbook -i ${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml ${UFO_SIMULATOR_ANSIBLE_DIR}/ipa.yml
ansible-playbook -i ${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml ${UFO_SIMULATOR_ANSIBLE_DIR}/kcm.yml
ansible-playbook -i ${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml ${UFO_SIMULATOR_ANSIBLE_DIR}/lvp.yml
ansible-playbook -i ${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml ${UFO_SIMULATOR_ANSIBLE_DIR}/metallb.yml
ansible-playbook -i ${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml ${UFO_SIMULATOR_ANSIBLE_DIR}/netris-controller.yml
ansible-playbook -i ${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml ${UFO_SIMULATOR_ANSIBLE_DIR}/netris-operator.yml
ansible-playbook -i ${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml ${UFO_SIMULATOR_ANSIBLE_DIR}/ufo.yml

# Wait everything is ready before moving forwad
kubectl wait --for=condition=Ready=True management/kcm --timeout=1800s
kubectl wait --for=condition=ready pod --all --all-namespaces --timeout=1800m

ansible-playbook -i ${UFO_SIMULATOR_ANSIBLE_DIR}/inventory.yml ${UFO_SIMULATOR_ANSIBLE_DIR}/configure-switches.yml

# Register resources
kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/static/site-default.yaml
kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/static/pxe-net.yaml
kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/static/subnetpool-default.yaml
kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/ctl.yaml
kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/leaf-0.yaml
kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/leaf-1.yaml
kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/netris_ipam.yaml
kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/spine-0.yaml
kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/spine-1.yaml
kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/vm-0.yaml
kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/vm-1.yaml
kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/vm-2.yaml
kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/vm-0_bmh.yaml
kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/vm-1_bmh.yaml
kubectl apply -f ${UFO_K8S_ARTIFACTS_DIR}/vm-2_bmh.yaml
