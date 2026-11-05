{{- define "rh.fullname" -}}
{{- if contains "rush-hour" .Release.Name -}}
{{- .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-rush-hour" .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* labels: call with (dict "root" $ "name" "catalog") */}}
{{- define "rh.selector" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
{{- end -}}

{{- define "rh.labels" -}}
{{ include "rh.selector" . }}
app.kubernetes.io/part-of: rush-hour
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version }}
{{- end -}}

{{- define "rh.dbSecret" -}}
{{- default (printf "%s-db" (include "rh.fullname" .)) .Values.database.existingSecret -}}
{{- end -}}

{{/* host the migration job (and the PgBouncer upstream) talks to: Postgres itself */}}
{{- define "rh.pgDirectHost" -}}
{{- if .Values.postgres.enabled -}}{{ include "rh.fullname" . }}-postgres{{- else -}}{{ required "database.host is required when postgres.enabled=false" .Values.database.host }}{{- end -}}
{{- end -}}

{{/* host the services talk to: PgBouncer when enabled */}}
{{- define "rh.pgAppHost" -}}
{{- if .Values.pgbouncer.enabled -}}{{ include "rh.fullname" . }}-pgbouncer{{- else -}}{{ include "rh.pgDirectHost" . }}{{- end -}}
{{- end -}}

{{- define "rh.redisUrl" -}}
{{- if .Values.redis.enabled -}}redis://{{ include "rh.fullname" . }}-redis:6379/0{{- else -}}{{ required "redis.externalUrl is required when redis.enabled=false" .Values.redis.externalUrl }}{{- end -}}
{{- end -}}

{{/* pod + container security shared by everything we run */}}
{{- define "rh.podSecurity" -}}
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "rh.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{/* spread pods of one app over nodes: call with (dict "root" $ "name" "catalog") */}}
{{- define "rh.spread" -}}
{{- /* ScheduleAnyway: spread across nodes when possible, never leave a pod Pending on a small cluster */ -}}
topologySpreadConstraints:
  - maxSkew: 1
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: ScheduleAnyway
    labelSelector:
      matchLabels:
        {{- include "rh.selector" . | nindent 8 }}
{{- end -}}

{{- define "rh.pgbouncerIni" -}}
[databases]
{{ .Values.database.name }} = host={{ include "rh.pgDirectHost" . }} port={{ .Values.database.port }} dbname={{ .Values.database.name }}

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 5432
unix_socket_dir =
auth_type = scram-sha-256
auth_file = /etc/pgbouncer-auth/userlist.txt
pool_mode = {{ .Values.pgbouncer.poolMode }}
max_client_conn = {{ .Values.pgbouncer.maxClientConn }}
default_pool_size = {{ .Values.pgbouncer.defaultPoolSize }}
ignore_startup_parameters = extra_float_digits,options
server_reset_query =
log_connections = 0
log_disconnections = 0
{{- end -}}
