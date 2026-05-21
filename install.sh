#!/usr/bin/env bash
# LogPose — Interactive OpenShift Install Script
# Prompts for all sensitive credentials; nothing is hard-coded.
set -euo pipefail

# ── colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'; CYN='\033[0;36m'
BLD='\033[1m'; RST='\033[0m'

info()  { echo -e "${CYN}${BLD}[INFO]${RST}  $*"; }
ok()    { echo -e "${GRN}${BLD}[ OK ]${RST}  $*"; }
warn()  { echo -e "${YLW}${BLD}[WARN]${RST}  $*"; }
die()   { echo -e "${RED}${BLD}[FAIL]${RST}  $*" >&2; exit 1; }
header(){ echo -e "\n${BLD}${CYN}═══ $* ═══${RST}"; }

# ── helpers ───────────────────────────────────────────────────────────────────
prompt() {
    # prompt <var_name> <display_label> [default]
    local var="$1" label="$2" default="${3:-}"
    local hint=""
    [[ -n "$default" ]] && hint=" [${default}]"
    read -r -p "$(echo -e "${BLD}${label}${hint}: ${RST}")" val
    val="${val:-$default}"
    [[ -z "$val" ]] && die "Required: $label"
    printf -v "$var" '%s' "$val"
}

prompt_secret() {
    # prompt_secret <var_name> <display_label>
    local var="$1" label="$2"
    local val=""
    while [[ -z "$val" ]]; do
        read -r -s -p "$(echo -e "${BLD}${label} (hidden): ${RST}")" val
        echo
        [[ -z "$val" ]] && warn "Value cannot be empty. Try again."
    done
    printf -v "$var" '%s' "$val"
}

confirm() {
    # confirm <message> — exits loop on y/n
    local msg="$1" ans=""
    while true; do
        read -r -p "$(echo -e "${BLD}${msg} [y/n]: ${RST}")" ans
        case "$ans" in
            [Yy]*) return 0 ;;
            [Nn]*) return 1 ;;
            *) warn "Please answer y or n." ;;
        esac
    done
}

check_tool() {
    command -v "$1" &>/dev/null || die "'$1' not found on PATH. Install it before running this script."
}

# ── 0. Prerequisites check ────────────────────────────────────────────────────
header "0. Checking prerequisites"
for tool in oc podman git; do
    check_tool "$tool"
    ok "$tool found"
done

# ── 1. Collect credentials interactively ─────────────────────────────────────
header "1. Credentials & configuration"
echo -e "${YLW}All secrets are collected now and passed directly to 'oc create secret'.${RST}"
echo -e "${YLW}They are never written to disk or stored in shell history.${RST}\n"

# RabbitMQ
echo -e "${BLD}── RabbitMQ ──${RST}"
prompt        RABBITMQ_USER   "RabbitMQ username"    "logpose"
prompt_secret RABBITMQ_PASS   "RabbitMQ password"

# AWS
echo -e "\n${BLD}── AWS (SQS consumer + CloudTrail enricher) ──${RST}"
prompt_secret AWS_ACCESS_KEY_ID     "AWS Access Key ID"
prompt_secret AWS_SECRET_ACCESS_KEY "AWS Secret Access Key"
prompt        AWS_REGION            "AWS Region"                  "us-east-1"
prompt        SQS_QUEUE_URL         "SQS Queue URL (full HTTPS)"

# Splunk
echo -e "\n${BLD}── Splunk HEC ──${RST}"
prompt        SPLUNK_HEC_URL   "Splunk HEC URL (e.g. https://splunk:8088/services/collector)"
prompt_secret SPLUNK_HEC_TOKEN "Splunk HEC Token"
prompt        SPLUNK_INDEX     "Splunk index"  "main"

# Optional: expose RabbitMQ management UI
EXPOSE_RMQ_MGMT=false
echo
if confirm "Expose RabbitMQ Management UI route (optional, useful for debugging)?"; then
    EXPOSE_RMQ_MGMT=true
fi

# ── 2. CRC startup ────────────────────────────────────────────────────────────
header "2. CRC — start cluster"
if confirm "Configure and start CRC now? (skip if already running)"; then
    info "Setting CRC resources: 6 vCPU, 16 GiB RAM, 60 GiB disk"
    crc config set cpus 6
    crc config set memory 16384
    crc config set disk-size 60
    crc setup
    crc start
fi

info "Activating oc from CRC environment"
eval "$(crc oc-env)"

info "Logging in as kubeadmin"
KUBEADMIN_PASS=$(crc console --credentials 2>/dev/null | grep kubeadmin | awk '{print $4}' || true)
if [[ -z "$KUBEADMIN_PASS" ]]; then
    prompt_secret KUBEADMIN_PASS "kubeadmin password (from 'crc console --credentials')"
fi
oc login -u kubeadmin -p "$KUBEADMIN_PASS" https://api.crc.testing:6443
ok "Logged in to OpenShift"

# Create or switch to the logpose namespace
if oc get project logpose &>/dev/null; then
    info "Project 'logpose' already exists — switching to it"
    oc project logpose
else
    info "Creating project 'logpose'"
    oc new-project logpose
fi
ok "Namespace: logpose"

# ── 3. Build & push image ─────────────────────────────────────────────────────
header "3. Build & push LogPose image"
HOST=$(oc registry info)
SHA=$(git rev-parse --short HEAD)
IMAGE_SHA="$HOST/logpose/logpose:$SHA"
IMAGE_LATEST="$HOST/logpose/logpose:latest"
INTERNAL_IMAGE="image-registry.openshift-image-registry.svc:5000/logpose/logpose:latest"

info "Registry: $HOST"
info "SHA tag:  $IMAGE_SHA"

info "Authenticating Podman to OpenShift internal registry"
oc whoami -t | podman login --username "$(oc whoami)" --password-stdin "$HOST"

info "Building image (linux/amd64)"
podman buildx build \
    --platform linux/amd64 \
    --tag "$IMAGE_SHA" \
    --tag "$IMAGE_LATEST" \
    .

info "Pushing SHA-pinned tag"
podman push --tls-verify=false "$IMAGE_SHA"
info "Pushing :latest tag"
podman push --tls-verify=false "$IMAGE_LATEST"
ok "Image pushed: $INTERNAL_IMAGE"

# Grant the default SA permission to pull from the internal registry
oc policy add-role-to-user system:image-puller \
    system:serviceaccount:logpose:default -n logpose 2>/dev/null || true

# ── 4. Secrets & ConfigMap ────────────────────────────────────────────────────
header "4. Secrets & ConfigMap"

# Remove existing secrets so we can recreate cleanly (idempotent)
for secret in logpose-rabbitmq logpose-aws logpose-splunk rabbitmq-default-user; do
    oc delete secret "$secret" --ignore-not-found
done
oc delete configmap logpose-config --ignore-not-found

info "Creating logpose-rabbitmq secret"
oc create secret generic logpose-rabbitmq \
    --from-literal="RABBITMQ_URL=amqp://${RABBITMQ_USER}:${RABBITMQ_PASS}@rabbitmq:5672/" \
    --from-literal="RABBITMQ_MGMT_URL=http://rabbitmq:15672" \
    --from-literal="RABBITMQ_USER=${RABBITMQ_USER}" \
    --from-literal="RABBITMQ_PASS=${RABBITMQ_PASS}"

info "Creating logpose-aws secret"
oc create secret generic logpose-aws \
    --from-literal="AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}" \
    --from-literal="AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}" \
    --from-literal="AWS_REGION=${AWS_REGION}" \
    --from-literal="SQS_QUEUE_URL=${SQS_QUEUE_URL}"

info "Creating logpose-splunk secret"
oc create secret generic logpose-splunk \
    --from-literal="SPLUNK_HEC_URL=${SPLUNK_HEC_URL}" \
    --from-literal="SPLUNK_HEC_TOKEN=${SPLUNK_HEC_TOKEN}" \
    --from-literal="SPLUNK_INDEX=${SPLUNK_INDEX}"

info "Creating logpose-config configmap"
oc create configmap logpose-config \
    --from-literal=SPLUNK_BATCH_SIZE=50 \
    --from-literal=METRICS_DB_PATH=/tmp/logpose_metrics.db \
    --from-literal=DASHBOARD_HOST=0.0.0.0 \
    --from-literal=DASHBOARD_PORT=8080

ok "All secrets and configmap created"

# ── 5. RabbitMQ StatefulSet ───────────────────────────────────────────────────
header "5. Deploy RabbitMQ"

info "Creating rabbitmq-default-user secret"
oc create secret generic rabbitmq-default-user \
    --from-literal="RABBITMQ_DEFAULT_USER=${RABBITMQ_USER}" \
    --from-literal="RABBITMQ_DEFAULT_PASS=${RABBITMQ_PASS}"

info "Applying RabbitMQ Service and StatefulSet"
oc apply -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: rabbitmq
spec:
  clusterIP: None
  selector:
    app: rabbitmq
  ports:
  - name: amqp
    port: 5672
    targetPort: 5672
  - name: mgmt
    port: 15672
    targetPort: 15672
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: rabbitmq
spec:
  serviceName: rabbitmq
  replicas: 1
  selector:
    matchLabels:
      app: rabbitmq
  template:
    metadata:
      labels:
        app: rabbitmq
    spec:
      containers:
      - name: rabbitmq
        image: rabbitmq:3-management
        ports:
        - containerPort: 5672
          name: amqp
        - containerPort: 15672
          name: mgmt
        envFrom:
        - secretRef:
            name: rabbitmq-default-user
        volumeMounts:
        - name: data
          mountPath: /var/lib/rabbitmq
        readinessProbe:
          exec:
            command: ["rabbitmq-diagnostics", "ping"]
          initialDelaySeconds: 15
          periodSeconds: 10
        livenessProbe:
          exec:
            command: ["rabbitmq-diagnostics", "ping"]
          initialDelaySeconds: 30
          periodSeconds: 30
  volumeClaimTemplates:
  - metadata:
      name: data
    spec:
      accessModes: ["ReadWriteOnce"]
      resources:
        requests:
          storage: 5Gi
EOF

info "Waiting for RabbitMQ to be ready (timeout 180s)"
oc rollout status statefulset/rabbitmq --timeout=180s
ok "RabbitMQ ready"

# ── 6. LogPose Workloads ──────────────────────────────────────────────────────
header "6. Deploy LogPose workloads"

info "Deploying SQS consumer"
oc apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: logpose-consumer-sqs
spec:
  replicas: 1
  selector: {matchLabels: {app: logpose-consumer-sqs}}
  template:
    metadata: {labels: {app: logpose-consumer-sqs}}
    spec:
      containers:
      - name: logpose
        image: ${INTERNAL_IMAGE}
        imagePullPolicy: Always
        command: ["python", "-m", "logpose.consumers.sqs_main"]
        env:
        - name: AWS_DEFAULT_REGION
          valueFrom:
            secretKeyRef: {name: logpose-aws, key: AWS_REGION}
        envFrom:
        - secretRef: {name: logpose-rabbitmq}
        - secretRef: {name: logpose-aws}
        - configMapRef: {name: logpose-config}
EOF

info "Deploying router"
oc apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: logpose-router
spec:
  replicas: 1
  selector: {matchLabels: {app: logpose-router}}
  template:
    metadata: {labels: {app: logpose-router}}
    spec:
      containers:
      - name: logpose
        image: ${INTERNAL_IMAGE}
        imagePullPolicy: Always
        command: ["python", "-m", "logpose.router_main"]
        envFrom:
        - secretRef: {name: logpose-rabbitmq}
        - configMapRef: {name: logpose-config}
EOF

info "Deploying CloudTrail runbook"
oc apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: logpose-runbook-cloudtrail
spec:
  replicas: 1
  selector: {matchLabels: {app: logpose-runbook-cloudtrail}}
  template:
    metadata: {labels: {app: logpose-runbook-cloudtrail}}
    spec:
      containers:
      - name: logpose
        image: ${INTERNAL_IMAGE}
        imagePullPolicy: Always
        command: ["python", "-m", "logpose.runbooks.cloud.aws"]
        env:
        - name: AWS_DEFAULT_REGION
          valueFrom:
            secretKeyRef: {name: logpose-aws, key: AWS_REGION}
        envFrom:
        - secretRef: {name: logpose-rabbitmq}
        - secretRef: {name: logpose-aws}
        - configMapRef: {name: logpose-config}
EOF

info "Deploying GCP Event Audit runbook"
oc apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: logpose-runbook-gcp-event-audit
spec:
  replicas: 1
  selector: {matchLabels: {app: logpose-runbook-gcp-event-audit}}
  template:
    metadata: {labels: {app: logpose-runbook-gcp-event-audit}}
    spec:
      containers:
      - name: logpose
        image: ${INTERNAL_IMAGE}
        imagePullPolicy: Always
        command: ["python", "-m", "logpose.runbooks.cloud.gcp"]
        envFrom:
        - secretRef: {name: logpose-rabbitmq}
        - configMapRef: {name: logpose-config}
EOF

info "Deploying test runbook"
oc apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: logpose-runbook-test
spec:
  replicas: 1
  selector: {matchLabels: {app: logpose-runbook-test}}
  template:
    metadata: {labels: {app: logpose-runbook-test}}
    spec:
      containers:
      - name: logpose
        image: ${INTERNAL_IMAGE}
        imagePullPolicy: Always
        command: ["python", "-m", "logpose.runbooks.test_runbook"]
        envFrom:
        - secretRef: {name: logpose-rabbitmq}
        - configMapRef: {name: logpose-config}
EOF

info "Deploying forwarder"
oc apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: logpose-forwarder
spec:
  replicas: 1
  selector: {matchLabels: {app: logpose-forwarder}}
  template:
    metadata: {labels: {app: logpose-forwarder}}
    spec:
      containers:
      - name: logpose
        image: ${INTERNAL_IMAGE}
        imagePullPolicy: Always
        command: ["python", "-m", "logpose.forwarder_main"]
        envFrom:
        - secretRef: {name: logpose-rabbitmq}
        - secretRef: {name: logpose-splunk}
        - configMapRef: {name: logpose-config}
EOF

info "Deploying dashboard"
oc apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: logpose-dashboard
spec:
  replicas: 1
  selector: {matchLabels: {app: logpose-dashboard}}
  template:
    metadata: {labels: {app: logpose-dashboard}}
    spec:
      containers:
      - name: logpose
        image: ${INTERNAL_IMAGE}
        imagePullPolicy: Always
        command: ["python", "-m", "logpose.dashboard_main"]
        ports:
        - containerPort: 8080
        envFrom:
        - secretRef: {name: logpose-rabbitmq}
        - configMapRef: {name: logpose-config}
---
apiVersion: v1
kind: Service
metadata:
  name: logpose-dashboard
spec:
  selector:
    app: logpose-dashboard
  ports:
  - port: 8080
    targetPort: 8080
EOF

info "Exposing dashboard route"
oc expose svc/logpose-dashboard 2>/dev/null || true
DASHBOARD_URL=$(oc get route logpose-dashboard -o jsonpath='{.spec.host}' 2>/dev/null || echo "(not yet assigned)")
ok "Dashboard URL: http://${DASHBOARD_URL}"

# Optional RabbitMQ management UI
if [[ "$EXPOSE_RMQ_MGMT" == "true" ]]; then
    info "Exposing RabbitMQ management UI"
    oc expose svc/rabbitmq --port=15672 --name=rabbitmq-mgmt 2>/dev/null || true
    RMQ_URL=$(oc get route rabbitmq-mgmt -o jsonpath='{.spec.host}' 2>/dev/null || echo "(not yet assigned)")
    ok "RabbitMQ management URL: http://${RMQ_URL}"
fi

# ── 7. Wait for rollouts ──────────────────────────────────────────────────────
header "7. Waiting for all rollouts"
DEPLOYMENTS=(
    logpose-consumer-sqs
    logpose-router
    logpose-runbook-cloudtrail
    logpose-runbook-gcp-event-audit
    logpose-runbook-test
    logpose-forwarder
    logpose-dashboard
)
for d in "${DEPLOYMENTS[@]}"; do
    info "Waiting for $d"
    oc rollout status "deployment/${d}" --timeout=120s
    ok "$d ready"
done

# ── 8. Summary ────────────────────────────────────────────────────────────────
header "Installation complete"
oc get pods
echo
ok "Dashboard:  http://${DASHBOARD_URL}"
[[ "$EXPOSE_RMQ_MGMT" == "true" ]] && ok "RabbitMQ:   http://${RMQ_URL}  (login: ${RABBITMQ_USER} / your password)"
echo
echo -e "${BLD}Smoke test:${RST}"
echo "  aws sqs send-message \\"
echo "    --queue-url \"\$SQS_QUEUE_URL\" \\"
echo "    --message-body '{\"_logpose_test\": true, \"hello\": \"logpose\"}'"
echo
echo -e "${YLW}See solution_installation.md §8 for full verification steps.${RST}"
