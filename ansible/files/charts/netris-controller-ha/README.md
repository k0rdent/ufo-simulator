# Netris Controller Helm Chart

* Installs the automatic NetOps platform [Netris Controller](https://www.netris.ai/overview/)

## Prerequisites

- Kubernetes 1.12+
- Helm 3.1+
- PV provisioner support in the underlying infrastructure


## Install Mariadb-Operator CRDs
Install Mariadb-Operator CRDs:

```
kubectl apply -f https://raw.githubusercontent.com/mariadb-operator/mariadb-operator/refs/tags/v0.37.1/deploy/crds/crds.yaml
```

## Get Repo Info
Add the Netris Helm repository:

```
helm repo add netrisai https://netrisai.github.io/charts
helm repo update
```

## Installing the Chart

In order to install the Helm chart, you must follow these steps:

Create the namespace for netris-controller:

```
kubectl create namespace netris-controller
```

Install helm chart with netris-controller

```
helm install netris-controller netrisai/netris-controller \
  --namespace netris-controller \
  --set ingress.hosts={my.domain.com}
```

## Uninstalling the Chart

To uninstall/delete the `netris-controller` helm release:

```
helm uninstall netris-controller
```

## Configuration

The following table lists the configurable parameters of the netris-controller chart and their default values.

### Common parameters

| Parameter                             | Description                                                                                               | Default                    |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------- |
| `nameOverride`                        | String to partially override common.names.fullname template with a string (will prepend the release name) | `""`                      |
| `fullnameOverride`                    | String to fully override common.names.fullname template with a string                                     | `""`                      |
| `serviceAccount.create`               | Create a serviceAccount for the deployment                                                                | `true`                     |
| `serviceAccount.name`                 | Use the serviceAccount with the specified name                                                            | `""`                       |
| `serviceAccount.annotations`          | Annotations to add to the service account                                                                 | `{}`                       |
| `podAnnotations`                      | Pod annotations                                                                                           | `{}`                       |
| `podSecurityContext`                  | Pod Security Context                                                                                      | `{}`                       |
| `securityContext`                     | Containers security context                                                                               | `{}`                       |
| `resources`                           | CPU/memory resource requests/limits                                                                       | `{}`                       |
| `nodeSelector`                        | Node labels for pod assignment                                                                            | `{}`                       |
| `tolerations`                         | Node tolerations for pod assignment                                                                       | `[]`                       |
| `affinity`                            | Node affinity for pod assignment                                                                          | `{}`                       |


### Netris-Controller common parameters

| Parameter                             | Description                                                                                               | Default                    |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------- |
| `netris.webLogin`                     | Netris Controller GUI default login                                                                       | `netris`                   |
| `netris.webPassword`                  | Netris Controller GUI default password                                                                    | `newNet0ps`                |

### Netris-Controller ingress resources parameters
| Parameter                             | Description                                                                                               | Default                    |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------- |
| `ingress.enabled`                     | Enables Ingress                                                                                           | `true`                     |
| `ingress.annotations`                 | Ingress annotations (values are templated)                                                                | `{}`                       |
| `ingress.labels`                      | Custom labels                                                                                             | `{}`                       |
| `ingress.path`                        | Ingress accepted path                                                                                     | `/`                        |
| `ingress.pathType`                    | Ingress type of path                                                                                      | `Prefix`                   |
| `ingress.hosts`                       | Ingress accepted hostnames                                                                                | `["chart-example.local"]`  |
| `ingress.tls`                         | Ingress TLS configuration                                                                                 | `[]`                       |


### Netris-Controller web-service-backend parameters
| Parameter                                                         | Description                                                                                               | Default                           |
| ------------------------------------------------------------------| --------------------------------------------------------------------------------------------------------- | --------------------------------- |
| `web-service-backend.replicaCount`                                | Number of replicas in web-service-backend deployment                                                      | `1`                               |
| `web-service-backend.image.repository`                            | Image repository                                                                                          | `netrisai/controller-web-service-backend` |
| `web-service-backend.image.tag`                                   | Image tag. Overrides the image tag whose default is the chart appVersion                                  | `"4.5.2-007"`                         |
| `web-service-backend.image.pullPolicy`                            | Image pull policy                                                                                         | `IfNotPresent`                    |
| `web-service-backend.imagePullSecrets`                            | Reference to one or more secrets to be used when pulling images                                           | `[]`                              |
| `web-service-backend.service.type`                                | Kubernetes service type 	                                                                                | `ClusterIP`                       |
| `web-service-backend.service.port`                                | Kubernetes port where service is expose 	                                                                | `80`                              |
| `web-service-backend.service.portName`                            | Name of the port on the service                                                                           | `http`                            |
| `web-service-backend.autoscaling.enabled`                         | Option to turn autoscaling on for app and specify params for HPA. Autoscaling needs metrics-server to access cpu metrics  | `false` |
| `web-service-backend.autoscaling.minReplicas`                     | Default min replicas for autoscaling                                                                      | `1`                              |
| `web-service-backend.autoscaling.maxReplicas`                     | Default max replicas for autoscaling                                                                      | `100`                            |
| `web-service-backend.autoscaling.targetCPUUtilizationPercentage`  | The desired target CPU utilization for autoscaling                                                        | `80`                             |
| `web-service-backend.nodeOptions`                                 | A space-separated list of Node.js command-line options                                                    | `--max-old-space-size=8192`      |


### Netris-Controller web-service-frontend parameters
| Parameter                                                         | Description                                                                                               | Default                           |
| ------------------------------------------------------------------| --------------------------------------------------------------------------------------------------------- | --------------------------------- |
| `web-service-frontend.replicaCount`                               | Number of replicas in web-service-frontend deployment                                                     | `1`                               |
| `web-service-frontend.image.repository`                           | Image repository                                                                                          | `netrisai/controller-web-service-frontend` |
| `web-service-frontend.image.tag`                                  | Image tag. Overrides the image tag whose default is the chart appVersion                                  | `"4.5.2-009"`                         |
| `web-service-frontend.image.pullPolicy`                           | Image pull policy                                                                                         | `IfNotPresent`                    |
| `web-service-frontend.imagePullSecrets`                           | Reference to one or more secrets to be used when pulling images                                           | `[]`                              |
| `web-service-frontend.service.type`                               | Kubernetes service type 	                                                                                | `ClusterIP`                       |
| `web-service-frontend.service.port`                               | Kubernetes port where service is expose 	                                                                | `80`                              |
| `web-service-frontend.service.portName`                           | Name of the port on the service                                                                           | `http`                            |
| `web-service-frontend.autoscaling.enabled`                        | Option to turn autoscaling on for app and specify params for HPA. Autoscaling needs metrics-server to access cpu metrics  | `false` |
| `web-service-frontend.autoscaling.minReplicas`                    | Default min replicas for autoscaling                                                                      | `1`                              |
| `web-service-frontend.autoscaling.maxReplicas`                    | Default max replicas for autoscaling                                                                      | `100`                            |
| `web-service-frontend.autoscaling.targetCPUUtilizationPercentage` | The desired target CPU utilization for autoscaling                                                        | `80`                             |


### Netris-Controller grpc parameters
| Parameter                              | Description                                                                                               | Default                    |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------- |
| `grpc.replicaCount`                    | Number of replicas in grpc deployment                                                                     | `1`                        |
| `grpc.image.repository`                | Image repository                                                                                          | `netrisai/controller-grpc` |
| `grpc.image.tag`                       | Image tag. Overrides the image tag whose default is the chart appVersion                                  | `"4.5.2.004"`              |
| `grpc.image.pullPolicy`                | Image pull policy                                                                                         | `IfNotPresent`             |
| `grpc.imagePullSecrets`                | Reference to one or more secrets to be used when pulling images                                           | `[]`                       |
| `grpc.service.type`                    | Kubernetes service type 	                                                                                 | `ClusterIP`                |
| `grpc.service.port`                    | Kubernetes port where service is expose 	                                                                 | `443`                      |
| `grpc.service.portName`                | Name of the port on the service                                                                           | `grpc`                     |
| `grpc.autoscaling.enabled`                         | Option to turn autoscaling on for app and specify params for HPA. Autoscaling needs metrics-server to access cpu metrics | `false`        |
| `grpc.autoscaling.minReplicas`                     | Default min replicas for autoscaling                                                                      | `1`                           |
| `grpc.autoscaling.maxReplicas`                     | Default max replicas for autoscaling                                                                      | `100`                         |
| `grpc.autoscaling.targetCPUUtilizationPercentage`  | The desired target CPU utilization for autoscaling                                                        | `80`                          |


### Netris-Controller telescope parameters
| Parameter                             | Description                                                                                               | Default                    |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------- |
| `telescope.replicaCount`                    | Number of replicas in telescope deployment                                                          | `1`                        |
| `telescope.image.repository`                | Image repository                                                                                          | `netrisai/controller-telescope` |
| `telescope.image.tag`                       | Image tag. Overrides the image tag whose default is the chart appVersion                                  | `"4.5.2.006"`                       |
| `telescope.image.pullPolicy`                | Image pull policy                                                                                         | `IfNotPresent`             |
| `telescope.imagePullSecrets`                | Reference to one or more secrets to be used when pulling images                                           | `[]`                       |
| `telescope.service.type`                    | Kubernetes service type 	                                                                                | `ClusterIP`                |
| `telescope.service.port`                    | Kubernetes port where service is expose 	                                                                | `80`                      |
| `telescope.service.portName`                | Name of the port on the service                                                                           | `ws`                     |
| `telescope.service.securePort`                    | Kubernetes secure port where service is expose 	                                                    | `443`                      |
| `telescope.service.securePortName`                | Name of the secure port on the service                                                              | `wss`                     |
| `telescope.autoscaling.enabled`                         | Option to turn autoscaling on for app and specify params for HPA. Autoscaling needs metrics-server to access cpu metrics | `false`        |
| `telescope.autoscaling.minReplicas`                     | Default min replicas for autoscaling                                                                      | `1`                           |
| `telescope.autoscaling.maxReplicas`                     | Default max replicas for autoscaling                                                                      | `100`                         |
| `telescope.autoscaling.targetCPUUtilizationPercentage`  | The desired target CPU utilization for autoscaling                                                        | `80`                          |


### Netris-Controller telescope-notifier parameters
| Parameter                             | Description                                                                                               | Default                    |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------- |
| `telescope-notifier.replicaCount`                    | Number of replicas in telescope-notifier deployment                                        | `1`                        |
| `telescope-notifier.image.repository`                | Image repository                                                                           | `netrisai/controller-telescope-notifier` |
| `telescope-notifier.image.tag`                       | Image tag. Overrides the image tag whose default is the chart appVersion                   | `"3.0.4"`                  |
| `telescope-notifier.image.pullPolicy`                | Image pull policy                                                                          | `IfNotPresent`             |
| `telescope-notifier.imagePullSecrets`                | Reference to one or more secrets to be used when pulling images                            | `[]`                       |
| `telescope-notifier.autoscaling.enabled`                         | Option to turn autoscaling on for app and specify params for HPA. Autoscaling needs metrics-server to access cpu metrics | `false`        |
| `telescope-notifier.autoscaling.minReplicas`                     | Default min replicas for autoscaling                                                                      | `1`                           |
| `telescope-notifier.autoscaling.maxReplicas`                     | Default max replicas for autoscaling                                                                      | `100`                         |
| `telescope-notifier.autoscaling.targetCPUUtilizationPercentage`  | The desired target CPU utilization for autoscaling                                                        | `80`                          |


### Netris-Controller web-session-generator parameters
| Parameter                                               | Description                                                                                               | Default                    |
| ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------- |
| `web-session-generator.replicaCount`                    | Number of replicas in web-session-generator deployment                                                    | `1`                        |
| `web-session-generator.image.repository`                | Image repository                                                                                          | `netrisai/controller-web-session-generator` |
| `web-session-generator.image.tag`                       | Image tag. Overrides the image tag whose default is the chart appVersion                                  | `"0.2.0"`                  |
| `web-session-generator.image.pullPolicy`                | Image pull policy                                                                                         | `IfNotPresent`             |
| `web-session-generator.imagePullSecrets`                | Reference to one or more secrets to be used when pulling images                                           | `[]`                       |
| `web-session-generator.service.type`                    | Kubernetes service type 	                                                                                | `ClusterIP`                |
| `web-session-generator.service.port`                    | Kubernetes port where service is expose 	                                                                | `80`                       |
| `web-session-generator.service.portName`                | Name of the port on the service                                                                           | `http`                     |
| `web-session-generator.autoscaling.enabled`                         | Option to turn autoscaling on for app and specify params for HPA. Autoscaling needs metrics-server to access cpu metrics | `false`        |
| `web-session-generator.autoscaling.minReplicas`                     | Default min replicas for autoscaling                                                                      | `1`                           |
| `web-session-generator.autoscaling.maxReplicas`                     | Default max replicas for autoscaling                                                                      | `100`                         |
| `web-session-generator.autoscaling.targetCPUUtilizationPercentage`  | The desired target CPU utilization for autoscaling                                                        | `80`                          |


### Netris-Controller equinix-metal-agent parameters
| Parameter                                             | Description                                                                                               | Default                    |
| ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------- |
| `equinix-metal-agent.enabled`                         | Enable equinix-metal-agent deployment                                                                     | `true`                     |
| `equinix-metal-agent.image.repository`                | Image repository                                                                                          | `netrisai/bare-metal-equinix-metal-agent` |
| `equinix-metal-agent.image.tag`                       | Image tag. Overrides the image tag whose default is the chart appVersion                                  | `"0.11.1"`                 |
| `equinix-metal-agent.image.pullPolicy`                | Image pull policy                                                                                         | `IfNotPresent`             |
| `equinix-metal-agent.imagePullSecrets`                | Reference to one or more secrets to be used when pulling images                                           | `[]`                       |


### Netris-Controller phoenixnap-bmc-agent parameters
| Parameter                                             | Description                                                                                               | Default                    |
| ----------------------------------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------- |
| `phoenixnap-bmc-agent.enabled`                         | Enable phoenixnap-bmc-agent deployment                                                                   | `true`                     |
| `phoenixnap-bmc-agent.image.repository`                | Image repository                                                                                         | `netrisai/bare-metal-phoenixnap-bmc-agent` |
| `phoenixnap-bmc-agent.image.tag`                       | Image tag. Overrides the image tag whose default is the chart appVersion                                 | `"0.6.1"`                  |
| `phoenixnap-bmc-agent.image.pullPolicy`                | Image pull policy                                                                                        | `IfNotPresent`             |
| `phoenixnap-bmc-agent.imagePullSecrets`                | Reference to one or more secrets to be used when pulling images                                          | `[]`                       |


### Netris-Controller migration parameters
| Parameter                        | Description                                                                   | Default                                     |
| ---------------------------------| ----------------------------------------------------------------------------- | ------------------------------------------- |
| `migration.image.repository`     | Image repository                                                              | `netrisai/controller-web-service-migration` |
| `migration.image.tag`            | Image tag. Overrides the image tag whose default is the chart appVersion      | `"4.5.2"`                                 |
| `migration.image.pullPolicy`     | Image pull policy                                                             | `IfNotPresent`                              |
| `migration.imagePullSecrets`     | Reference to one or more secrets to be used when pulling images               | `[]`                                        |


### Mariadb parameters
_Using default values [from](https://github.com/bitnami/charts/tree/master/bitnami/mariadb/values.yaml)_

| Parameter                             | Description                                                                                               | Default                    |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------- |
| `mariadb.initdbScriptsConfigMap`      | ConfigMap with the initdb scripts.                                                                        | `netris-controller-initdb` |
| `mariadb.auth.database`               | Name for a database to create                                                                             | `netris`                   |
| `mariadb.auth.username`               | Name for a user to create                                                                                 | `netris`                   |
| `mariadb.auth.password`               | Password for the new user                                                                                 | `changeme`                 |
| `mariadb.auth.rootPassword`           | Password for the root user                                                                                | `changeme`                 |

_Auth from existing secret not supported at the momment_


### Mongodb parameters
_Using default values [from](https://github.com/bitnami/charts/tree/master/bitnami/mongodb/values.yaml)_

| Parameter                             | Description                                                                                               | Default                    |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------- |
| `mongodb.useStatefulSet`              | Use StatefulSet instead of Deployment when deploying standalone                                           | `true`                     |   
| `mongodb.initdbScriptsConfigMap`      | ConfigMap with the initdb scripts.                                                                        | `netris-controller-initdb-mongodb` |
| `mongodb.auth.database`               | Name for a database to create                                                                             | `netris`                   |
| `mongodb.auth.username`               | Name for a user to create                                                                                 | `netris`                   |
| `mongodb.auth.password`               | Password for the new user                                                                                 | `changeme`                 |
| `mongodb.auth.rootPassword`           | Password for the root user                                                                                | `changeme`                 |

_Auth from existing secret not supported at the momment_


### Redis parameters
_Using default values [from](https://github.com/bitnami/charts/tree/master/bitnami/redis/values.yaml)_

| Parameter                             | Description                                                                                               | Default                    |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------- |
| `redis.cluster.enabled`               | Use master-slave topology                                                                                 | `false`                    |
| `redis.usePassword`                   | Use password                                                                                              | `false`                    |

_Auth not supported at the momment_



### Smtp parameters
_Using default values [from](https://github.com/ntppool/charts/tree/main/charts/smtp/values.yaml)_

| Parameter                             | Description                                                                                               | Default                    |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------- |
| `smtp.config.DISABLE_IPV6`            | Disable IPv6                                                                                              | `1`                        |
| `smtp.config.RELAY_NETWORKS`          | Relay networks. Change if your CNI use other subnets                                                      | `:172.16.0.0/12:10.0.0.0/8:192.168.0.0/16` |


### Graphite parameters
_Using default values [from](https://github.com/kiwigrid/helm-charts/tree/master/charts/graphite/values.yaml)_

| Parameter                             | Description                                                                                               | Default                    |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------- |
| `graphite.configMaps`                 | Netris-Controller supported graphite's config files                                                       | `see in values.yaml`       |
| `graphite.service.type`               | Kubernetes service type                                                                                   | `ClusterIP`                |


## Usage
Specify each parameter using the --set key=value[,key=value] argument to helm install. For example,

```
helm install netris-controller netrisai/netris-controller \
  --namespace netris-controller \
  --set ingress.hosts={my.domain.com} \
  --set mariadb.auth.rootPassword=my-root-password \
  --set mariadb.auth.password=my-password \
  --set mongodb.auth.rootPassword=my-root-password \
  --set mongodb.auth.password=my-password
```

The above command sets netris-controller application ingress host to `my.domain.com`. Additionally, it sets MariaDB and MongoDB root account password to `my-root-password` and user account password to `my-password`.

Alternatively, a YAML file that specifies the values for the parameters can be provided while installing the chart. For example,

```
helm install netris-controller netrisai/netris-controller --namespace netris-controller -f values.yaml 
```

Also you can see overrides values from helm get values 
```
helm get values netris-controller
```
