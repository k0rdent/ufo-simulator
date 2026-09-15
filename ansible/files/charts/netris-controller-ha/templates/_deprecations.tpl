{{- define "deprecations" -}}
{{- $deprecated := list -}}
{{/* add templates here */}}
{{- $deprecated = append $deprecated (include "deprecate.appIngress" .) -}}
{{- $deprecated = append $deprecated (include "deprecate.app" .) -}}

{{- /* prepare output */}}
{{- $deprecated = without $deprecated "" -}}
{{- $message := join "\n" $deprecated -}}

{{- /* print output */}}
{{- if $message -}}
{{-   printf "\nDEPRECATIONS:\n%s" $message | fail -}}
{{- end -}}
{{- end -}}


{{/* Deprecation behaviors for .app.ingress */}}
{{- define "deprecate.appIngress" -}}
{{- if .Values.app }}
{{- if hasKey .Values.app "ingress" -}}
app.ingress:
  The configuration of `app.ingress` has been renamed. Please use `ingress` instead.
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Deprecation behaviors for .app */}}
{{- define "deprecate.app" -}}
{{- if .Values.app }}
app:
  The configuration of `app` has been renamed. Please use `web-service-backend` or `web-service-frontend` instead.
{{- end -}}
{{- end -}}
