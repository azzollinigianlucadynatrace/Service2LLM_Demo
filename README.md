# Dynatrace + OpenTelemetry LLM OpenAI Demo (Python) — **Multi‑Service Traces + Metrics**

This demo shows how to manually instrument raw REST LLM calls with OpenTelemetry spans (including GenAI semantic attributes) and export traces/metrics to Dynatrace via OTLP, plus how to **correctly propagate trace context across services** (including the critical propagator setup and header case normalization) so you can correlate end‑to‑end request paths.
**OpenAI API based but easily replicable with other LLM Providers**

This README walks you end‑to‑end through running the demo:

1) create an **OpenAI API key**,
2) create a **Dynatrace API token**,
3) install prerequisites,
4) run the two services,
5) generate traffic and verify **traces, metrics, endpoints, and (optionally) service flow**.

> **What you get:**
> - **Service A (Gateway)**: `POST /chat` ➜ calls Service B
> - **Service B (Worker)**: `POST /work` ➜ calls **OpenAI** via raw REST (manual span instrumentation)
> - **OpenTelemetry traces** exported via **OTLP/HTTP** to Dynatrace
> - **OpenTelemetry metrics** exported via **OTLP/HTTP** to Dynatrace

---

## 0) Prerequisites

- **Python 3.8+** installed.
- A **Dynatrace SaaS** environment (tenant) and permission to create an API token.
- An **OpenAI account** to create an API key.

Dynatrace provides native OTLP ingest endpoints for traces and metrics using the standard OTLP paths (`/v1/traces`, `/v1/metrics`) under the Dynatrace OTLP base URL. 

---

## 1) Create an OpenAI API key

1. Sign in to the OpenAI developer platform and open the **API Keys** page.
2. Click **Create new secret key** and copy it immediately (you won't see it again). 

**Useful links:**
- OpenAI API keys page: [platform.openai.com/account/api-keys]
- OpenAI quickstart (key creation + env var): [platform.openai.com/docs/quickstart]

**Security tip:** treat the key like a password; don't commit it to git or paste it into public places.

---

## 2) Create a Dynatrace API token

You need a Dynatrace token with these scopes:

- `openTelemetryTrace.ingest` — required to ingest OTLP traces
- `metrics.ingest` — required to ingest OTLP metrics

**Steps (Dynatrace UI):**
1. Go to **Settings → Access tokens**.
2. Create a new token and enable the permissions above.
3. Copy the token.

---

## 3) Get your Dynatrace OTLP endpoints

Dynatrace OTLP endpoints use a base URL like:

```text
https://{your-environment-id}.live.dynatrace.com/api/v2/otlp
```

In this demo we use the **signal-specific** endpoints:

```text
Traces:  https://{env}.live.dynatrace.com/api/v2/otlp/v1/traces
Metrics: https://{env}.live.dynatrace.com/api/v2/otlp/v1/metrics
```

---

## 4) Download / prepare the demo code

**Project structure (expected):**

```text
.
├─ common_otel.py
├─ service_a_gateway.py
├─ service_b_worker.py
├─ load_test_client.py
├─ requirements.txt
└─ .env
```

---

## 5) Create a virtual environment and install dependencies

From the project directory:

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

---

## 6) Create your `.env` file

Create a `.env` file in the project root.

### 6.1 Minimal `.env`

```bash
# Dynatrace
DYNATRACE_ENVIRONMENT_URL=https://{your-environment-id}.live.dynatrace.com
DYNATRACE_API_TOKEN=dt0c01.XXXX.XXXX

# OpenAI
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...

# Optional: content capture flags (be careful with sensitive data)
OTEL_CAPTURE_PROMPTS=true
OTEL_CAPTURE_COMPLETIONS=false

# Service URLs
WORKER_URL=http://127.0.0.1:5002/work
GATEWAY_URL=http://127.0.0.1:5001/chat
```

### 6.2 Recommended (propagation safety)

Dynatrace relies on **W3C Trace Context** (`traceparent`/`tracestate`) to keep end‑to‑end visibility across services.

You can explicitly set OTel propagators (optional but helpful for debugging):

```bash
OTEL_PROPAGATORS=tracecontext,baggage
```

---

## 7) IMPORTANT: Metrics temporality (Using DELTA)

Dynatrace OTLP metrics ingest ** delta temporality** for counters and histograms. 
This demo already enforces DELTA for Counters/Histograms in code (in `common_otel.py`) using exporter preferred temporality.

---

## 8) Run the demo (two terminals)

> Make sure the virtual environment is activated in **each** terminal.

### Terminal 1 — start Worker (Service B)

```bash
source venv/bin/activate
python service_b_worker.py
```

You should see:

```text
Worker running on http://127.0.0.1:5002/work
```

### Terminal 2 — start Gateway (Service A)

```bash
source venv/bin/activate
python service_a_gateway.py
```

You should see:

```text
Gateway running on http://127.0.0.1:5001/chat  -> worker: http://127.0.0.1:5002/work
```

---

## 9) Generate traffic (3 prompts, 1 round)

### Terminal 3 — run the client

```bash
source venv/bin/activate
python load_test_client.py
```

This sends 3 prompts (1 round) and stops.

---

## 10) Verify in Dynatrace

### 10.1 Traces

1. Go to **Distributed traces**.
2. Filter by service name:
   - `llm-demo-gateway`
   - `llm-demo-worker`

### 10.2 Endpoints

Dynatrace creates endpoints (entry points) from **SERVER** spans.
You should see endpoints like:
- `POST /chat` (Gateway)
- `POST /work` (Worker)

### 10.3 AI / GenAI spans

In the Worker trace, open the span named `llm.chat.completions` and inspect GenAI attributes:

- `gen_ai.system`
- `gen_ai.request.model`
- `gen_ai.usage.total_tokens`

### 10.4 Metrics

Go to **Metrics / Data explorer** and search for these demo metric names:

- `service.requests`
- `service.errors`
- `service.latency.ms`
- `llm.requests`
- `llm.errors`
- `llm.latency.ms`
- `llm.tokens.used`

Note: Dynatrace may suffix metric keys based on payload (for example counters may appear with `.count`).

---

## 11) CRITICAL: Making Service Flow Connect Gateway ➜ Worker

Service-to-service visualization requires correct **trace context propagation** across the HTTP call between Gateway and Worker.

### Requirements

There are **three critical requirements** for trace propagation to work:

#### Requirement 1: Propagator Setup

The W3C TraceContext propagator **must** be explicitly configured in `common_otel.py`:

```python
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator

# MUST be called before creating tracer
propagator = CompositePropagator([
    TraceContextTextMapPropagator(),
    W3CBaggagePropagator(),
])
set_global_textmap(propagator)
```

**Without this, `inject()` does nothing and no `traceparent` header is added!**

#### Requirement 2: Gateway Injects Context

In Gateway, inject context into outbound headers:

```python
headers = {"Content-Type": "application/json"}
inject(headers)
# headers now contains: {'Content-Type': '...', 'traceparent': '00-...'}
```

#### Requirement 3: Worker Extracts Context (with lowercase headers!)

In Worker, extract context from inbound headers. **Critical: normalize header keys to lowercase!**

```python
# CORRECT - Flask capitalizes headers, but W3C propagator expects lowercase
carrier = {key.lower(): value for key, value in request.headers}
ctx = extract(carrier)

# WRONG - will fail because Flask sends "Traceparent" not "traceparent"
ctx = extract(request.headers)  # ❌
carrier = {key: value for key, value in request.headers}  # ❌ (still capitalized)
```

### Verification

1. Open a trace for `POST /chat` and verify spans from both services appear in the **same trace**.
2. If you see two separate traces, check:
   - Is propagator configured? (add `print(headers)` after `inject()`)
   - Are header keys lowercase? (add `print(carrier)` before `extract()`)

---

