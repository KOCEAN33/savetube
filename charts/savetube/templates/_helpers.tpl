{{- define "savetube.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "savetube.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "savetube.labels" -}}
{{ include "savetube.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "savetube.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{- define "savetube.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: [ALL]
{{- end -}}

{{/* Native sidecar. No auth, so it binds loopback and only its own pod reaches it. */}}
{{- define "savetube.potProvider" -}}
- name: pot-provider
  image: {{ .Values.potProvider.image.repository }}:{{ .Values.potProvider.image.tag }}
  restartPolicy: Always
  args: ["--host", "127.0.0.1"]
  securityContext:
    allowPrivilegeEscalation: false
    capabilities:
      drop: [ALL]
  resources:
    {{- toYaml .Values.potProvider.resources | nindent 4 }}
{{- end -}}

{{/* Env every savetube container carries. */}}
{{- define "savetube.dbEnv" -}}
- name: SAVETUBE_LOG_LEVEL
  value: {{ .Values.logLevel | quote }}
- name: SAVETUBE_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.key }}
{{- end -}}

{{/* The cookie profiles, when cookies.existingSecret is set: every key
`<name>.txt` of the Secret is the profile `<name>`. Only the pods that run
yt-dlp mount them. The internet-facing web pod never does. */}}
{{- define "savetube.cookiesEnv" -}}
- name: SAVETUBE_COOKIES_DIR
  value: /secrets/cookies
{{- end -}}

{{- define "savetube.cookiesMount" -}}
- name: cookies
  mountPath: /secrets/cookies
  readOnly: true
{{- end -}}

{{- define "savetube.cookiesVolume" -}}
- name: cookies
  secret:
    secretName: {{ .Values.cookies.existingSecret }}
{{- end -}}

{{/* Env for a container that downloads or reads the archive tree. */}}
{{- define "savetube.archiveEnv" -}}
{{ include "savetube.dbEnv" . }}
- name: SAVETUBE_ARCHIVE_ROOT
  value: /archive
{{- if .Values.cookies.existingSecret }}
{{ include "savetube.cookiesEnv" . }}
{{- end }}
{{- end -}}

{{- define "savetube.archiveMounts" -}}
- name: archive
  mountPath: /archive
- name: tmp
  mountPath: /tmp
{{- if .Values.cookies.existingSecret }}
{{ include "savetube.cookiesMount" . }}
{{- end }}
{{- end -}}

{{- define "savetube.archiveVolumes" -}}
- name: archive
  persistentVolumeClaim:
    claimName: {{ required "archive.existingClaim is required" .Values.archive.existingClaim }}
- name: tmp
  emptyDir: {}
{{- if .Values.cookies.existingSecret }}
{{ include "savetube.cookiesVolume" . }}
{{- end }}
{{- end -}}

{{- define "savetube.podCommon" -}}
securityContext:
  {{- toYaml .Values.podSecurityContext | nindent 2 }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* GET /healthz on the process's asyncio loop: a stuck loop fails it. */}}
{{- define "savetube.livenessProbe" -}}
livenessProbe:
  httpGet:
    path: /healthz
    port: health
  initialDelaySeconds: 10
  periodSeconds: 10
  failureThreshold: 3
{{- end }}
