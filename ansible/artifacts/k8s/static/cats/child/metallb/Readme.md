# Install metallb

```
kubectl create ns metallb
helm repo add metallb https://metallb.github.io/metallb
helm install metallb metallb/metallb -n metallb
```
