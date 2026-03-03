import os
import time
import requests
from flask import Flask, request, jsonify

from opentelemetry import trace, metrics
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader, AggregationTemporality
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

# CRITICAL: Propagator imports
from opentelemetry.propagate import inject, set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator

from opentelemetry.trace import SpanKind
from opentelemetry.trace.status import Status, StatusCode

from dotenv import load_dotenv
load_dotenv()

# ============================================================
# SETUP OTEL WITH PROPAGATOR - THIS IS THE KEY FIX
# ============================================================
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "llm-demo-gateway")
DT_URL = os.getenv("DYNATRACE_ENVIRONMENT_URL")
DT_TOKEN = os.getenv("DYNATRACE_API_TOKEN")

WORKER_URL = os.getenv("WORKER_URL", "http://127.0.0.1:5002/work")
WORKER_SERVICE_NAME = os.getenv("WORKER_SERVICE_NAME", "llm-demo-worker")
WORKER_HOST = os.getenv("WORKER_HOST", "127.0.0.1")
WORKER_PORT = int(os.getenv("WORKER_PORT", "5002"))

if not DT_URL or not DT_TOKEN:
    raise RuntimeError("Missing DYNATRACE_ENVIRONMENT_URL or DYNATRACE_API_TOKEN")

# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
# CRITICAL FIX: Set up W3C TraceContext propagator FIRST
# This MUST happen before any tracing calls
# >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
propagator = CompositePropagator([
    TraceContextTextMapPropagator(),
    W3CBaggagePropagator(),
])
set_global_textmap(propagator)
print(f"[GATEWAY] Propagator configured: {propagator}")
# <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

# Resource
resource = Resource.create({
    "service.name": SERVICE_NAME,
    "service.version": "1.0.0",
    "deployment.environment": "local-demo",
})

# Tracing
tracer_provider = TracerProvider(resource=resource)
trace_exporter = OTLPSpanExporter(
    endpoint=f"{DT_URL}/api/v2/otlp/v1/traces",
    headers={"Authorization": f"Api-Token {DT_TOKEN}"},
)
tracer_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
trace.set_tracer_provider(tracer_provider)

# Metrics (with DELTA temporality for Dynatrace)
def _get_preferred_temporality():
    try:
        from opentelemetry.sdk.metrics._internal.instrument import Counter, Histogram, ObservableCounter, UpDownCounter, ObservableUpDownCounter
        return {
            Counter: AggregationTemporality.DELTA,
            ObservableCounter: AggregationTemporality.DELTA,
            Histogram: AggregationTemporality.DELTA,
            UpDownCounter: AggregationTemporality.CUMULATIVE,
            ObservableUpDownCounter: AggregationTemporality.CUMULATIVE,
        }
    except:
        return {}

metric_exporter = OTLPMetricExporter(
    endpoint=f"{DT_URL}/api/v2/otlp/v1/metrics",
    headers={"Authorization": f"Api-Token {DT_TOKEN}"},
    preferred_temporality=_get_preferred_temporality(),
)
meter_provider = MeterProvider(resource=resource, metric_readers=[PeriodicExportingMetricReader(metric_exporter, export_interval_millis=5000)])
metrics.set_meter_provider(meter_provider)

tracer = trace.get_tracer(SERVICE_NAME)
meter = metrics.get_meter(SERVICE_NAME)

# Metrics
svc_requests = meter.create_counter("service.requests")
svc_errors = meter.create_counter("service.errors")
svc_latency = meter.create_histogram("service.latency.ms")

app = Flask(__name__)


@app.route("/chat", methods=["POST"])
def chat():
    start = time.time()
    svc_requests.add(1, {"service": SERVICE_NAME, "endpoint": "/chat"})

    payload = request.get_json(silent=True) or {}
    prompt = payload.get("prompt", "")

    if not prompt:
        svc_errors.add(1, {"service": SERVICE_NAME, "endpoint": "/chat", "error.type": "validation"})
        return jsonify({"error": "Missing 'prompt'"}), 400

    with tracer.start_as_current_span("POST /chat", kind=SpanKind.SERVER) as span:
        span.set_attribute("http.route", "/chat")
        span.set_attribute("http.request.method", "POST")

        try:
            with tracer.start_as_current_span("POST /work", kind=SpanKind.CLIENT) as child:
                child.set_attribute("peer.service", WORKER_SERVICE_NAME)
                child.set_attribute("server.address", WORKER_HOST)
                child.set_attribute("server.port", WORKER_PORT)
                child.set_attribute("http.method", "POST")
                child.set_attribute("http.url", WORKER_URL)

                headers = {"Content-Type": "application/json"}
                
                # INJECT TRACE CONTEXT INTO HEADERS
                inject(headers)
                
                # DEBUG: Verify traceparent was added
                print(f"[GATEWAY] Headers after inject: {headers}")
                if "traceparent" not in headers:
                    print("[GATEWAY] ERROR: traceparent NOT injected!")
                
                resp = requests.post(WORKER_URL, headers=headers, json={"prompt": prompt}, timeout=30)
                child.set_attribute("http.status_code", resp.status_code)

                if resp.status_code >= 400:
                    child.set_status(Status(StatusCode.ERROR))
                    svc_errors.add(1, {"service": SERVICE_NAME, "endpoint": "/chat", "error.type": "downstream"})
                    return jsonify({"error": "Worker error", "details": resp.text}), 502

                return jsonify(resp.json()), 200

        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR))
            svc_errors.add(1, {"service": SERVICE_NAME, "endpoint": "/chat", "error.type": type(e).__name__})
            return jsonify({"error": str(e)}), 500
        finally:
            svc_latency.record(int((time.time() - start) * 1000), {"service": SERVICE_NAME, "endpoint": "/chat"})


if __name__ == "__main__":
    print(f"Gateway running on http://127.0.0.1:5001/chat -> worker: {WORKER_URL}")
    app.run(host="127.0.0.1", port=5001, debug=False)
