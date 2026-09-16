# Vendored chart: netris-controller-ha

| | |
|---|---|
| Chart version | `0.13.0-alpha.0` |
| Controller (appVersion) | `4.16.0-alpha.0` |
| Source | `netris-controller-ha-v4.16.0-002.tar.gz`, `files/charts/netris-controller-ha-0.13.0-alpha.0.tgz` |

## Why this chart is vendored

Netris froze the non-HA `netris-controller` chart at `2.8.2 / appVersion 4.6.0.2`. Its installer
version table (`https://get.netris.io`) maps every controller release from 4.7.0 onward to a
`netris-controller-ha` chart:

```
"4.6.0")  CONTROLLER_CHART_VERSION="2.8.1" CONTROLLER_HA_CHART_VERSION="0.3.0-alpha.1"
...
"4.16.0") CONTROLLER_CHART_VERSION="2.8.1" CONTROLLER_HA_CHART_VERSION="0.13.0-alpha.0"
```

The HA chart is **not published anywhere public** — `netrisai/charts@main` has no
`netris-controller-ha` directory, and the tarball 404s on GitHub releases. The only copy ships inside
the air-gapped controller bundle, so it lives here.

Subcharts (`mongodb`, `redis`, `smtp`, `victoria-metrics-cluster`) are already vendored under
`charts/`, so **no `helm dep up` is needed**.

## Mirantis delta

The lab runs a single node, so everything is scaled to one replica. Nearly all of that is achievable
through values — these were the only exceptions, all in `templates/mariadb-crs.yaml`, which hardcoded
replica counts with no value behind them.

All defaults below reproduce the upstream behaviour, so the patch is inert unless a values file opts
in. `ansible/templates/k8s/netris/netris_controller.yaml` is what opts in.

### 1. `templates/mariadb-crs.yaml` — `MariaDB` replicas, storage, replication

```diff
   storage:
-    size: 10Gi
+    size: {{ .Values.mariadb.storage.size | default "10Gi" }}
   image: docker-registry1.mariadb.com/library/mariadb:10.11.16
-  replicas: 3
-  replication:
-    enabled: true
-    syncBinlog: 1
+  replicas: {{ .Values.mariadb.replicas | default 3 }}
+  {{- if gt (int (.Values.mariadb.replicas | default 3)) 1 }}
+  replication:
+    enabled: true
+    syncBinlog: 1
+  {{- end }}
```

The `replication` guard matters: mariadb-operator's webhook rejects `replication.enabled` on a
single-replica `MariaDB`.

### 2. `templates/mariadb-crs.yaml` — `MaxScale` replicas

```diff
-  replicas: 3
+  replicas: {{ .Values.maxscale.replicas | default 3 }}
```

### 3. `templates/mariadb-crs.yaml` — number of `Backup` CRs

Each `Backup` claims a 20Gi PVC, and LVP only provides 15 static volumes for the whole cluster.

```diff
 {{- $dot := . }}
-{{- range $index := until 3 }}
+{{- range $index := until (int ($dot.Values.mariadb.backupCount | default 3)) }}
```

### 4. `values.yaml` — declare the new keys

Added `mariadb.replicas`, `mariadb.backupCount`, `mariadb.storage.size` and a new top-level
`maxscale.replicas`, all at the upstream defaults (3 / 3 / 10Gi / 3). Declaring them keeps the
template expressions nil-safe and the knobs discoverable.

## The release name must be `netris-controller-ha`

`netris-controller.fullname` returns the release name when it contains the chart name, and
`<release>-<chart>` otherwise. The bundled subcharts always name themselves `<release>-<subchart>`,
so the two only agree when the release name contains `netris-controller-ha`.

Installing as release `netris-controller` makes the controller look for
`netris-controller-netris-controller-ha-mongodb` while the subchart creates `netris-controller-mongodb`.
The web-service-backend pod then fails with `CreateContainerConfigError` on the missing secret, and
its redis, smtp and vmselect references are wrong for the same reason. The vendor's own manifests
install this chart as release `netris-controller-ha`; `ansible/netris-controller.yml` does the same.

## Do NOT scale MongoDB below 3

`templates/web-service-backend.yaml` builds `CONDUCTOR_MONGO_URL` with `mongodb-0`, `mongodb-1`,
`mongodb-2` and `replicaSet=rs0` hardcoded. A smaller replica set leaves the backend pointing at
hostnames that never resolve. `mongodb.replicaCount` must stay at 3.

## Re-vendoring a newer chart

1. Unpack the new `netris-controller-ha-<version>.tgz` over this directory.
2. Re-apply the four hunks above (check whether upstream has since made any of them values-driven).
3. Update the version table at the top of this file.
4. `helm template` with `ansible/templates/k8s/netris/netris_controller.yaml` and confirm exactly one
   `MariaDB` (no `replication:`), one `MaxScale`, one `Backup`.
