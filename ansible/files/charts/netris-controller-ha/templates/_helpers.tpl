{{/*
Expand the name of the chart.
*/}}
{{- define "netris-controller.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "netris-controller.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "netris-controller.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "netris-controller.labels" -}}
helm.sh/chart: {{ include "netris-controller.chart" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "netris-controller.selectorLabels" -}}
app.kubernetes.io/name: {{ include "netris-controller.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "netris-controller.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "netris-controller.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Generate certificates for netris-controller api server 
*/}}
{{- define "netris-controller.gen-certs" -}}
{{- $altNames := list ( printf "%s.%s" "*" .Release.Namespace ) ( printf "%s.%s.svc" "*" .Release.Namespace ) -}}
{{- $ca := genCA "netris-controller-ca" 365 -}}
{{- $cert := genSignedCert ( include "netris-controller.name" . ) nil $altNames 365 $ca -}}
tls.crt: {{ $cert.Cert | b64enc }}
tls.key: {{ $cert.Key | b64enc }}
{{- end -}}

{{/*
Generate a web-session-secret secret or use the existing one
*/}}
{{- define "web.session.secret" -}}
{{- $secret := lookup "v1" "Secret" .Release.Namespace (printf "%s-%s" (include "netris-controller.fullname" .) "web-session-secret") -}}
{{- if $secret }}
secret-key: {{ (index $secret.data "secret-key") }}
{{- else }}
secret-key: {{ randAlphaNum 40 | b64enc }}
{{- end }}
{{- end }}

{{/*
Generate a grpc-secret secret or use the existing one
*/}}
{{- define "grpc.secret" -}}
{{- $secret := lookup "v1" "Secret" .Release.Namespace (printf "%s-%s" (include "netris-controller.fullname" .) "grpc-secret") -}}
{{- if $secret }}
secret-key: {{ (index $secret.data "secret-key") }}
{{- else }}
secret-key: {{ randAlphaNum 40 | b64enc }}
{{- end }}
{{- end }}

{{/*
Render hook annotations for Argo CD or Helm based on .Values.argocd.enabled.
Pass a dict with "root" (the chart context) and "annotations" (values map with argocd/helm keys).
*/}}
{{- define "netris-controller.hookAnnotations" -}}
{{- $root := .root -}}
{{- $annotations := .annotations -}}
{{- if default false $root.Values.argocd.enabled -}}
{{- toYaml $annotations.argocd -}}
{{- else -}}
{{- toYaml $annotations.helm -}}
{{- end -}}
{{- end -}}
