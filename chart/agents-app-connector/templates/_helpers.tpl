{{- define "agents-app-connector.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "agents-app-connector.fullname" -}}
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

{{- define "agents-app-connector.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "agents-app-connector.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agents-app-connector.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "agents-app-connector.labels" -}}
helm.sh/chart: {{ include "agents-app-connector.chart" . }}
{{ include "agents-app-connector.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Call with (dict "context" . "component" "app"). */}}
{{- define "agents-app-connector.componentLabels" -}}
{{ include "agents-app-connector.selectorLabels" .context }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "agents-app-connector.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "agents-app-connector.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end }}

{{/* imagePullSecrets from values plus global, deduplicated by name. */}}
{{- define "agents-app-connector.imagePullSecrets" -}}
{{- $g := ((.Values.global | default dict).imagePullSecrets) | default list }}
{{- $seen := dict }}
{{- $all := list }}
{{- range concat (.Values.imagePullSecrets | default list) $g }}
{{- if not (hasKey $seen .name) }}
{{- $_ := set $seen .name true }}
{{- $all = append $all . }}
{{- end }}
{{- end }}
{{- if $all }}
imagePullSecrets:
  {{- toYaml $all | nindent 2 }}
{{- end }}
{{- end }}

{{/* Name of the Secret holding the database URL; fails when no source is set. */}}
{{- define "agents-app-connector.databaseSecretName" -}}
{{- $p := .Values.postgres -}}
{{- if and $p.url $p.existingSecret -}}
{{- fail "agents-app-connector: set only one of postgres.url and postgres.existingSecret" -}}
{{- else if $p.existingSecret -}}
{{- $p.existingSecret -}}
{{- else if $p.url -}}
{{- include "agents-app-connector.fullname" . }}-database
{{- else -}}
{{- fail "agents-app-connector: a Postgres connection is required: set postgres.url or postgres.existingSecret" -}}
{{- end -}}
{{- end }}

{{- define "agents-app-connector.databaseSecretKey" -}}
{{- if .Values.postgres.existingSecret -}}
{{- .Values.postgres.existingSecretKey | default "uri" -}}
{{- else -}}
uri
{{- end -}}
{{- end }}

{{/* Name of the Secret holding the orchestrator token; fails unless exactly one source is set. */}}
{{- define "agents-app-connector.tokenSecretName" -}}
{{- $o := .Values.orchestrator -}}
{{- if and $o.token $o.existingSecret -}}
{{- fail "agents-app-connector: set only one of orchestrator.token and orchestrator.existingSecret" -}}
{{- else if $o.existingSecret -}}
{{- $o.existingSecret -}}
{{- else if $o.token -}}
{{- include "agents-app-connector.fullname" . }}-orchestrator
{{- else -}}
{{- fail "agents-app-connector: the orchestrator token is required: set orchestrator.token or orchestrator.existingSecret" -}}
{{- end -}}
{{- end }}

{{- define "agents-app-connector.tokenSecretKey" -}}
{{- if .Values.orchestrator.existingSecret -}}
{{- .Values.orchestrator.existingSecretKey | default "token" -}}
{{- else -}}
token
{{- end -}}
{{- end }}

{{/* Name of the PVC the app pod mounts. */}}
{{- define "agents-app-connector.claimName" -}}
{{- default (printf "%s-data" (include "agents-app-connector.fullname" .)) .Values.persistence.existingClaim -}}
{{- end }}

{{/*
The app is a singleton (in-process rate limiter, advisory-locked worker, ReadWriteOnce PVC):
fails rendering when a replica count other than 1 is set.
*/}}
{{- define "agents-app-connector.appReplicas" -}}
{{- $r := 1 -}}
{{- if hasKey .Values.components.app "replicas" -}}
{{- $r = .Values.components.app.replicas | int -}}
{{- end -}}
{{- if ne $r 1 -}}
{{- fail "agents-app-connector: components.app is a singleton and must have replicas 1" -}}
{{- end -}}
1
{{- end }}

{{/* Container env: settings ConfigMap, database URL, and for admin and app the orchestrator token. */}}
{{- define "agents-app-connector.env" -}}
envFrom:
  - configMapRef:
      name: {{ include "agents-app-connector.fullname" .context }}-settings
env:
  - name: APPCONN_DATABASE_URL
    valueFrom:
      secretKeyRef:
        name: {{ include "agents-app-connector.databaseSecretName" .context }}
        key: {{ include "agents-app-connector.databaseSecretKey" .context }}
{{- if ne .component "migrate" }}
  - name: APPCONN_ORCHESTRATOR_TOKEN
    valueFrom:
      secretKeyRef:
        name: {{ include "agents-app-connector.tokenSecretName" .context }}
        key: {{ include "agents-app-connector.tokenSecretKey" .context }}
{{- end }}
{{- if eq .component "admin" }}
  - name: APPCONN_ADMIN_ALLOWED_CIDRS
    value: {{ join "," (.context.Values.adminAllowedCidrs | default (list "127.0.0.1/32")) | quote }}
{{- end }}
{{- if eq .component "app" }}
  - name: APPCONN_DATA_DIR
    value: /data
{{- end }}
{{- end }}

{{/* Settings ConfigMap data, no secret values. Fails on a bad env key or a missing url. */}}
{{- define "agents-app-connector.config" -}}
APPCONN_ORCHESTRATOR_URL: {{ required "orchestrator.url is required" .Values.orchestrator.url | quote }}
APPCONN_HTTP_PORT: {{ .Values.components.admin.port | int | toString | quote }}
APPCONN_DEVICE_PORT: {{ .Values.components.app.port | int | toString | quote }}
{{- range $k, $v := .Values.env }}
{{- if not (hasPrefix "APPCONN_" $k) }}
{{- fail (printf "agents-app-connector: env key %q must start with APPCONN_" $k) }}
{{- end }}
{{ $k }}: {{ $v | toString | quote }}
{{- end }}
{{- end }}

{{/* Validates the Secret inputs even when no Deployment includes the env. */}}
{{- define "agents-app-connector.validate" -}}
{{- $_ := include "agents-app-connector.databaseSecretName" . }}
{{- $_ := include "agents-app-connector.tokenSecretName" . }}
{{- $_ := include "agents-app-connector.appReplicas" . }}
{{- end }}
