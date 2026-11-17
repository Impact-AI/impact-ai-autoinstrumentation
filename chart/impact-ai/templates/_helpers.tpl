{{- /* SPDX-License-Identifier: Apache-2.0 */ -}}
{{/*
Names. The kit is installed once per cluster, so cluster-scoped objects have fixed names: a second install (another
release, another namespace) meets the first one's objects, and the guard below says so instead of Helm's ownership error.
*/}}
{{- define "cluster-kit.clusterRoleName" -}}impact-ai-kit-reporter{{- end -}}
{{- define "cluster-kit.reporterName" -}}{{ .Release.Name }}-reporter{{- end -}}
{{- define "cluster-kit.gatewayName" -}}{{ .Release.Name }}-gateway{{- end -}}
{{- define "cluster-kit.uninstallName" -}}{{ .Release.Name }}-uninstall{{- end -}}
{{- define "cluster-kit.coverName" -}}{{ .Release.Name }}-cover{{- end -}}
{{- define "cluster-kit.exchangeName" -}}{{ .Release.Name }}-exchange{{- end -}}
{{- define "cluster-kit.installTokenName" -}}{{ .Release.Name }}-install-token{{- end -}}

{{/*
The install token, trimmed, or empty. Read defensively: a release made before the value existed has no `install` at all.
*/}}
{{- define "cluster-kit.installToken" -}}
{{- dig "install" "token" "" .Values.AsMap | toString | trim -}}
{{- end -}}

{{/* "true" when an install token gives the cluster its key, empty when the customer does (ingest.key, ingest.existingSecret). */}}
{{- define "cluster-kit.tokenInstall" -}}
{{- if include "cluster-kit.installToken" . -}}true{{- end -}}
{{- end -}}

{{/*
The Secret that holds the ingest key. With an install token the exchange hook makes it, under a name of its own: the
Secret of a pasted key belongs to Helm, which deletes it when it leaves the manifest, and the key a hook wrote must
survive an upgrade from a pasted key to a token.
*/}}
{{- define "cluster-kit.keySecretName" -}}
{{- if include "cluster-kit.tokenInstall" . -}}
{{- printf "%s-install-key" .Release.Name -}}
{{- else -}}
{{- default (printf "%s-ingest-key" .Release.Name) .Values.ingest.existingSecret -}}
{{- end -}}
{{- end -}}
{{- define "cluster-kit.operatorName" -}}{{ .Values.operator.fullnameOverride | default "impact-ai-operator" }}{{- end -}}
{{- define "cluster-kit.operatorWebhookService" -}}{{ include "cluster-kit.operatorName" . }}-webhook{{- end -}}
{{- define "cluster-kit.instrumentationName" -}}impact{{- end -}}

{{- define "cluster-kit.labels" -}}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: impact-ai
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{- define "cluster-kit.selectorLabels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
{{- end -}}

{{/*
The labels of an object of the kit that is cluster-scoped, which is no workload and so has no selector labels: the kit's,
and the release it belongs to. scripts/uninstall-cluster-kit.sh deletes what an uninstall that was cut short leaves behind
by these two labels, for the release it is given; the objects of a hook are not Helm's to delete then.
*/}}
{{- define "cluster-kit.clusterLabels" -}}
{{ include "cluster-kit.labels" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The ingest origin without a trailing slash. It has to be https: the ingest key, and the install token of the exchange, go
to it as bearer tokens. A test against a mock ingest may use http with ingest.allowInsecureHttp (the reporter, the gateway,
the exchange and the cover pod are told, and the reporter refuses an http endpoint that was not allowed on purpose).
*/}}
{{- define "cluster-kit.ingestOrigin" -}}
{{- $origin := required "ingest.endpoint is required: the origin of the impact.ai ingest host" .Values.ingest.endpoint | trimSuffix "/" -}}
{{- if and (hasPrefix "http://" (lower $origin)) (not .Values.ingest.allowInsecureHttp) -}}
{{- fail (printf "ingest.endpoint %s is plain http: the ingest key and the install token would cross the network in clear. Use the https:// address of the ingest host. For a test against a mock ingest only, set ingest.allowInsecureHttp=true" $origin) -}}
{{- end -}}
{{- $origin -}}
{{- end -}}

{{/*
An image reference. repository + tag, or repository@digest, or repository:tag@digest. A registry host is replaced by
global.imageRegistry when that is set. Also used by the vendored Operator chart, for its own image and for every image
its arguments name (scripts/vendor_operator_chart.py).
*/}}
{{- define "cluster-kit.imageReference" -}}
{{- $repository := .repository -}}
{{- if .registry -}}
{{- $repository = printf "%s/%s" (trimSuffix "/" .registry) (join "/" (rest (splitList "/" .repository))) -}}
{{- end -}}
{{- if and .tag .digest -}}{{ printf "%s:%s@%s" $repository .tag .digest }}
{{- else if .digest -}}{{ printf "%s@%s" $repository .digest }}
{{- else if .tag -}}{{ printf "%s:%s" $repository .tag }}
{{- else -}}{{ fail (printf "image %s has neither a tag nor a digest" .repository) }}
{{- end -}}
{{- end -}}

{{- define "cluster-kit.image" -}}
{{- $image := index .root.Values.images .name -}}
{{- include "cluster-kit.imageReference" (dict "repository" $image.repository "tag" $image.tag "digest" $image.digest "registry" .root.Values.global.imageRegistry) -}}
{{- end -}}

{{- define "cluster-kit.pullSecrets" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
{{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{- define "cluster-kit.podPlacement" -}}
{{- with .Values.nodeSelector }}
nodeSelector:
{{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
{{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.priorityClassName }}
priorityClassName: {{ . | quote }}
{{- end }}
{{- end -}}

{{/*
GOMEMLIMIT of the gateway: 80% of gateway.resources.limits.memory, in MiB (in bytes below 1 MiB). With it the Go runtime
collects garbage before the container reaches its limit, which is what the memory limiter processor's documentation asks
for; without it the gateway was OOM-killed in 1 of 3 runs of an ingest outage under load. Renders nothing, and so sets no
variable, when there is no memory limit or it is not a plain quantity (123456, 256Mi, 1.5Gi, 512M). Helm reads a number
in values as a float, so a limit written as a number is printed without an exponent first.
*/}}
{{- define "cluster-kit.gatewayGoMemLimit" -}}
{{- $raw := (.Values.gateway.resources.limits | default dict).memory | default "" -}}
{{- $limit := ternary (printf "%.0f" $raw) (toString $raw) (kindIs "float64" $raw) -}}
{{- if regexMatch "^[0-9]+(\\.[0-9]+)?(Ki|Mi|Gi|Ti|k|M|G|T)?$" $limit -}}
{{- $unit := regexReplaceAll "^[0-9.]+" $limit "" -}}
{{- $factors := dict "" 1.0 "k" 1000.0 "M" 1000000.0 "G" 1000000000.0 "T" 1000000000000.0 "Ki" 1024.0 "Mi" 1048576.0 "Gi" 1073741824.0 "Ti" 1099511627776.0 -}}
{{- $soft := mulf (mulf (float64 (regexReplaceAll "[^0-9.]" $limit "")) (get $factors $unit)) 0.8 | floor -}}
{{- if ge $soft 1048576.0 -}}
{{- printf "%dMiB" (int64 (floor (divf $soft 1048576.0))) -}}
{{- else if gt $soft 0.0 -}}
{{- printf "%d" (int64 $soft) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
A fingerprint of the ingest key, in the pod template of everything that reads it: the gateway takes the key from its
environment at start, and the reporter would otherwise wait for the kubelet to refresh the mounted file. A new key then
restarts both with `helm upgrade --install`. Of a key the exchange makes the fingerprint is that of the install token
(the first 16 hex characters of its sha256, which the exchange stores in the Secret as well): a new token is a new key, and
the same token again, as `--reset-then-reuse-values` hands it over, rolls nothing. Of a Secret the customer manages the fingerprint
is that of what the cluster holds now (empty under `helm template`, which has no cluster).
*/}}
{{- define "cluster-kit.keyChecksum" -}}
{{- if include "cluster-kit.tokenInstall" . -}}
{{- include "cluster-kit.installToken" . | sha256sum | trunc 16 -}}
{{- else if .Values.ingest.existingSecret -}}
{{- $secret := lookup "v1" "Secret" .Release.Namespace .Values.ingest.existingSecret -}}
{{- if $secret -}}
{{- get ($secret.data | default dict) "ingest-key" | sha256sum | trunc 16 -}}
{{- end -}}
{{- else -}}
{{- .Values.ingest.key | trim | sha256sum | trunc 16 -}}
{{- end -}}
{{- end -}}

{{/* The cluster's ID: the UID of kube-system. */}}
{{- define "cluster-kit.clusterUID" -}}
{{- if .Values.cluster.uid -}}
{{- .Values.cluster.uid -}}
{{- else -}}
{{- $namespace := lookup "v1" "Namespace" "" "kube-system" -}}
{{- if $namespace -}}
{{- $namespace.metadata.uid -}}
{{- else -}}
{{- fail "cluster.uid is required when the chart cannot query the cluster (helm template, Argo CD): --set cluster.uid=$(kubectl get namespace kube-system -o jsonpath='{.metadata.uid}')" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
A value written into the gateway's Collector configuration, with every "$" written as "$$". The Collector reads ${...} in
every string of its configuration before it parses anything ("${env:NAME}" is the variable, "${file:PATH}" the file,
"${https://host/path}" whatever that URL answers), and "$$" is its way to write a literal "$". A cluster name or ARN is free
text from the command that installs the kit, so every value goes through here and reaches the spans as the text it was.
*/}}
{{- define "cluster-kit.collectorLiteral" -}}
{{- . | toString | replace "$" "$$" -}}
{{- end -}}

{{/*
The reporter's flags for the cloud identity the installer gave (cloud.*, cluster.arn): one line each, nothing for a value
that is empty. The reporter and the uninstall notice take the same flags, so the final snapshot names the same cluster.
*/}}
{{- define "cluster-kit.cloudArgs" -}}
{{- $cloud := .Values.cloud | default dict -}}
{{- with $cloud.provider }}
- {{ printf "--cloud-provider=%s" . | quote }}
{{- end }}
{{- with $cloud.accountId }}
- {{ printf "--cloud-account-id=%s" . | quote }}
{{- end }}
{{- with $cloud.region }}
- {{ printf "--cloud-region=%s" . | quote }}
{{- end }}
{{- with .Values.cluster.arn }}
- {{ printf "--cluster-arn=%s" . | quote }}
{{- end }}
{{- end -}}

{{/* Namespaces the kit never covers: the system namespaces, this release's, and namespaces.exclude. */}}
{{- define "cluster-kit.excludedNamespaces" -}}
{{- $names := concat (list "kube-system" "kube-public" "kube-node-lease" .Release.Namespace) (.Values.namespaces.exclude | default list) -}}
{{- uniq $names | toJson -}}
{{- end -}}

{{/* The namespaceSelector of the Operator's webhooks, from namespaces.include / namespaces.exclude. */}}
{{- define "cluster-kit.namespaceSelector" -}}
matchExpressions:
  - key: kubernetes.io/metadata.name
    operator: NotIn
    values: {{ include "cluster-kit.excludedNamespaces" . }}
{{- if .Values.namespaces.include }}
  - key: kubernetes.io/metadata.name
    operator: In
    values: {{ .Values.namespaces.include | toJson }}
{{- end }}
{{- end -}}

{{/*
Another injector that already owns the cluster: aws-cloudwatch, datadog, dynatrace or opentelemetry-operator, or empty.
Read from the cluster's mutating webhook configurations when the chart is installed or upgraded (lookup returns
nothing under helm template, so the Operator is installed there). The reporter applies the same signatures at run time
(reporter/internal/injector); keep the two in step. Configurations of this release are skipped.
*/}}
{{- define "cluster-kit.foreignInjector" -}}
{{- $release := .Release -}}
{{- $found := dict -}}
{{- range (lookup "admissionregistration.k8s.io/v1" "MutatingWebhookConfiguration" "" "").items | default list -}}
{{- $annotations := .metadata.annotations | default dict -}}
{{- if not (and (eq (get $annotations "meta.helm.sh/release-name") $release.Name) (eq (get $annotations "meta.helm.sh/release-namespace") $release.Namespace)) -}}
{{- $config := .metadata.name -}}
{{- range .webhooks | default list -}}
{{- $service := (.clientConfig.service | default dict) -}}
{{- if or (contains "cloudwatch-observability" $config) (contains "amazon-cloudwatch" $config) (eq (get $service "namespace") "amazon-cloudwatch") -}}{{- $_ := set $found "aws-cloudwatch" true -}}{{- end -}}
{{- if or (eq $config "datadog-webhook") (hasPrefix "datadog.webhook." .name) -}}{{- $_ := set $found "datadog" true -}}{{- end -}}
{{- if or (eq $config "dynatrace-webhook") (hasSuffix ".dynatrace.com" .name) -}}{{- $_ := set $found "dynatrace" true -}}{{- end -}}
{{- if or (eq .name "mpod.kb.io") (eq .name "minstrumentation.kb.io") (eq (get $service "path") "/mutate-opentelemetry-io-v1alpha1-instrumentation") (and (eq (get $service "path") "/mutate-v1-pod") (contains "opentelemetry-operator" (get $service "name"))) -}}{{- $_ := set $found "opentelemetry-operator" true -}}{{- end -}}
{{- end -}}
{{- if or (contains "cloudwatch-observability" $config) (contains "amazon-cloudwatch" $config) -}}{{- $_ := set $found "aws-cloudwatch" true -}}{{- end -}}
{{- if eq $config "datadog-webhook" -}}{{- $_ := set $found "datadog" true -}}{{- end -}}
{{- if eq $config "dynatrace-webhook" -}}{{- $_ := set $found "dynatrace" true -}}{{- end -}}
{{- end -}}
{{- end -}}
{{- range $owner := list "aws-cloudwatch" "datadog" "dynatrace" "opentelemetry-operator" -}}
{{- if and (hasKey $found $owner) (not (hasKey $found "chosen")) -}}{{- $_ := set $found "chosen" $owner -}}{{- end -}}
{{- end -}}
{{- get $found "chosen" -}}
{{- end -}}

{{/* Whether this release installs the Operator and the Instrumentation resource (operator.install). */}}
{{- define "cluster-kit.operatorEnabled" -}}
{{- $mode := "auto" -}}
{{- if hasKey .Values.operator "install" -}}{{- $mode = toString .Values.operator.install -}}{{- end -}}
{{- if eq $mode "true" -}}true
{{- else if and (eq $mode "auto") (not (include "cluster-kit.foreignInjector" .)) -}}true
{{- end -}}
{{- end -}}

{{/*
One cluster reports to one product. If the kit is already installed (found through its cluster-wide ClusterRole, which
carries the product and the release), a different product or a different release is refused and the first is named.
*/}}
{{- define "cluster-kit.guard" -}}
{{- $role := lookup "rbac.authorization.k8s.io/v1" "ClusterRole" "" (include "cluster-kit.clusterRoleName" .) -}}
{{- if $role -}}
{{- $annotations := $role.metadata.annotations | default dict -}}
{{- $product := get $annotations "impact.ai/product-id" -}}
{{- $release := get $annotations "meta.helm.sh/release-name" -}}
{{- $namespace := get $annotations "meta.helm.sh/release-namespace" -}}
{{- if and $product (ne $product .Values.product.id) -}}
{{- fail (printf "this cluster is already connected to the impact.ai product %q (release %s in namespace %s). A cluster reports to one product. Uninstall that release first (helm uninstall %s -n %s), then install again for product %q." $product $release $namespace $release $namespace .Values.product.id) -}}
{{- end -}}
{{- if or (and $release (ne $release .Release.Name)) (and $namespace (ne $namespace .Release.Namespace)) -}}
{{- fail (printf "this cluster already has the impact.ai cluster kit: release %s in namespace %s (product %q). Upgrade that release instead of installing a second one: helm upgrade %s -n %s ..." $release $namespace $product $release $namespace) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Automatic coverage as one word: auto, true or false (the value may be written as a word or as a boolean).
*/}}
{{- define "cluster-kit.coverMode" -}}
{{- dig "cover" "enabled" "auto" .Values.AsMap | toString -}}
{{- end -}}

{{/*
Whether this release installs the cover pod: cover.enabled says yes (true, or auto with an install token and no image
mirror), and the Operator is installed, because nobody else would inject what the cover pod asks for. Behind a mirror
(global.imageRegistry) `auto` leaves the cover pod out: the kit's own pods pull from the mirror with the release's pull
secrets, but the workloads it would cover are in other namespaces and may not, and the init container the Operator adds
to their pods would wait in ImagePullBackOff at their next rollout. `true` says that they do.
*/}}
{{- define "cluster-kit.coverEnabled" -}}
{{- if include "cluster-kit.operatorEnabled" . -}}
{{- $mode := include "cluster-kit.coverMode" . -}}
{{- $mirror := dig "global" "imageRegistry" "" .Values.AsMap | toString -}}
{{- if or (eq $mode "true") (and (eq $mode "auto") (include "cluster-kit.tokenInstall" .) (not $mirror)) -}}true{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Whether the reporter adds the cover pod's marker and the impact.ai/* labels to its snapshots. Only when the cover pod is
installed or an install token was used: a server that does not know those fields never sees them.
*/}}
{{- define "cluster-kit.coverFields" -}}
{{- if or (include "cluster-kit.coverEnabled" .) (include "cluster-kit.tokenInstall" .) -}}true{{- end -}}
{{- end -}}

{{/* The namespaceSelector of the cover pod's webhook: the Operator's, and not the namespaces that opted out. */}}
{{- define "cluster-kit.coverNamespaceSelector" -}}
matchExpressions:
  - key: kubernetes.io/metadata.name
    operator: NotIn
    values: {{ include "cluster-kit.excludedNamespaces" . }}
{{- if .Values.namespaces.include }}
  - key: kubernetes.io/metadata.name
    operator: In
    values: {{ .Values.namespaces.include | toJson }}
{{- end }}
  - key: impact.ai/opt-out
    operator: DoesNotExist
{{- end -}}

{{/*
The certificate of a webhook the kit serves, as JSON of base64 strings (tls.crt, tls.key, ca.crt): the CA bundle of the
webhook configuration and the certificate its pod serves, made together. An install or upgrade reuses the Secret the chart
made earlier when that was issued for the same service (the Secret's impact.ai/service annotation says for which), so the
CA bundle and the certificate stay the same and the webhook keeps answering across `helm upgrade`. A new certificate on
every upgrade would leave the pod serving the old one until the kubelet had refreshed the mounted Secret, and for that
minute the API server could not verify it: the webhook would fail open and pods would start uninjected. The first
install (and `helm template`, which has no cluster to look in) makes a new one, valid for ten years. No cert-manager.
Called with root, service, secret (the Secret's name) and ca (the CA's common name).
*/}}
{{- define "cluster-kit.webhookCertificate" -}}
{{- $namespace := .root.Release.Namespace -}}
{{- $existing := lookup "v1" "Secret" $namespace .secret -}}
{{- $data := dict -}}
{{- $annotations := dict -}}
{{- if $existing -}}{{- $annotations = (index $existing "metadata").annotations | default dict -}}{{- end -}}
{{- if and $existing $existing.data (hasKey $existing.data "tls.crt") (hasKey $existing.data "tls.key") (hasKey $existing.data "ca.crt") (eq (get $annotations "impact.ai/service") (printf "%s.%s" .service $namespace)) -}}
{{- $data = dict "tls.crt" (index $existing.data "tls.crt") "tls.key" (index $existing.data "tls.key") "ca.crt" (index $existing.data "ca.crt") -}}
{{- else -}}
{{- $names := list .service (printf "%s.%s" .service $namespace) (printf "%s.%s.svc" .service $namespace) (printf "%s.%s.svc.cluster.local" .service $namespace) -}}
{{- $ca := genCA .ca 3650 -}}
{{- $cert := genSignedCert (printf "%s.%s.svc" .service $namespace) nil $names 3650 $ca -}}
{{- $data = dict "tls.crt" ($cert.Cert | b64enc) "tls.key" ($cert.Key | b64enc) "ca.crt" ($ca.Cert | b64enc) -}}
{{- end -}}
{{- toJson $data -}}
{{- end -}}

{{/* The cover pod's webhook certificate. */}}
{{- define "cluster-kit.coverCertificate" -}}
{{- $service := include "cluster-kit.coverName" . -}}
{{- include "cluster-kit.webhookCertificate" (dict "root" . "service" $service "secret" (printf "%s-cert" $service) "ca" (printf "%s-ca" $service)) -}}
{{- end -}}

{{/* The Operator's webhook certificate. */}}
{{- define "cluster-kit.operatorCertificate" -}}
{{- include "cluster-kit.webhookCertificate" (dict "root" . "service" (include "cluster-kit.operatorWebhookService" .) "secret" .Values.operator.admissionWebhooks.secretName "ca" "impact-ai-operator-ca") -}}
{{- end -}}

{{/*
Whether the Instrumentation resource gets the kit's Node.js image: the release has written one (images.nodejs) and the
Operator is installed. A chart without one (a checkout of this repository) leaves the section out, and the Operator's own
defaulting then fills in its upstream Node.js image on the Instrumentation.
*/}}
{{- define "cluster-kit.nodejsEnabled" -}}
{{- $image := dig "images" "nodejs" (dict) .Values.AsMap -}}
{{- if and (include "cluster-kit.operatorEnabled" .) (or (get $image "tag") (get $image "digest")) -}}true{{- end -}}
{{- end -}}

{{/*
The environment of an injected container, for either language: where the gateway is, and whether the workload trusts
the user, session and use case its callers send in baggage (IMPACT_TRUST_CALLER_BAGGAGE, the value of the pod's
annotation impact.ai/trust-caller-baggage through the downward API; empty, so not trusted, when the pod has none). The
Operator adds OTEL_TRACES_EXPORTER, OTEL_METRICS_EXPORTER, OTEL_LOGS_EXPORTER, OTEL_EXPORTER_OTLP_PROTOCOL and
OTEL_SERVICE_NAME to a workload that has not set them; each is defined here as an optional reference to a ConfigMap key
that does not exist, so Kubernetes leaves the variable out (see instrumentation.yaml).
*/}}
{{- define "cluster-kit.instrumentationEnv" -}}
- name: IMPACT_OTLP_ENDPOINT
  value: {{ printf "http://%s.%s.svc:4318" (include "cluster-kit.gatewayName" .) .Release.Namespace | quote }}
- name: IMPACT_TRUST_CALLER_BAGGAGE
  valueFrom:
    fieldRef:
      fieldPath: "metadata.annotations['impact.ai/trust-caller-baggage']"
{{- range $name := list "OTEL_TRACES_EXPORTER" "OTEL_METRICS_EXPORTER" "OTEL_LOGS_EXPORTER" "OTEL_EXPORTER_OTLP_PROTOCOL" "OTEL_SERVICE_NAME" }}
- name: {{ $name }}
  valueFrom:
    configMapKeyRef:
      name: impact-ai-unset
      key: unset
      optional: true
{{- end }}
{{- end -}}
