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
from opentelemetry.propagate import extract, set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.baggage.propagation import W3CBaggagePropagator

from opentelemetry.trace import SpanKind
from opentelemetry.trace.status import Status, StatusCode

from dotenv import load_dotenv
load_dotenv()

# ============================================================
# SETUP OTEL WITH PROPAGATOR
# ============================================================
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "llm-demo-worker")
DT_URL = os.getenv("DYNATRACE_ENVIRONMENT_URL")
DT_TOKEN = os.getenv("DYNATRACE_API_TOKEN")

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")

CAPTURE_PROMPTS = os.getenv("OTEL_CAPTURE_PROMPTS", "false").lower() == "true"
CAPTURE_COMPLETIONS = os.getenv("OTEL_CAPTURE_COMPLETIONS", "false").lower() == "true"

if not DT_URL or not DT_TOKEN:
    raise RuntimeError("Missing DYNATRACE_ENVIRONMENT_URL or DYNATRACE_API_TOKEN")

# Set up W3C TraceContext propagator
propagator = CompositePropagator([
    TraceContextTextMapPropagator(),
    W3CBaggagePropagator(),
])
set_global_textmap(propagator)
print(f"[WORKER] Propagator configured: {propagator}")

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

# Metrics
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
llm_requests = meter.create_counter("llm.requests")
llm_errors = meter.create_counter("llm.errors")
llm_latency = meter.create_histogram("llm.latency.ms")
llm_tokens_used = meter.create_counter("llm.tokens.used")

app = Flask(__name__)


def call_openai(prompt: str):
    model = "gpt-3.5-turbo"
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 100, "temperature": 0.7}

    start = time.time()
    resp = requests.post(url, headers=headers, json=body, timeout=60)
    dur_ms = int((time.time() - start) * 1000)

    if resp.status_code != 200:
        return None, model, dur_ms, {"status_code": resp.status_code, "error": resp.text}

    data = resp.json()
    completion = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return {
        "completion": completion,
        "finish_reason": data["choices"][0].get("finish_reason", "stop"),
        "prompt_tokens": int(usage.get("prompt_tokens", 0)),
        "completion_tokens": int(usage.get("completion_tokens", 0)),
        "total_tokens": int(usage.get("total_tokens", 0)),
        "provider": "openai",
        "model": model,
    }, model, dur_ms, None


def call_claude(prompt: str):
    model = "claude-3-haiku-20240307"
    url = "https://api.anthropic.com/v1/messages"
    headers = {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    body = {"model": model, "max_tokens": 100, "messages": [{"role": "user", "content": prompt}]}

    start = time.time()
    resp = requests.post(url, headers=headers, json=body, timeout=60)
    dur_ms = int((time.time() - start) * 1000)

    if resp.status_code != 200:
        return None, model, dur_ms, {"status_code": resp.status_code, "error": resp.text}

    data = resp.json()
    usage = data.get("usage", {})
    return {
        "completion": data["content"][0]["text"],
        "finish_reason": data.get("stop_reason", "end_turn"),
        "prompt_tokens": int(usage.get("input_tokens", 0)),
        "completion_tokens": int(usage.get("output_tokens", 0)),
        "total_tokens": int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0)),
        "provider": "anthropic",
        "model": model,
    }, model, dur_ms, None


@app.route("/work", methods=["POST"])
def work():
    svc_start = time.time()
    svc_requests.add(1, {"service": SERVICE_NAME, "endpoint": "/work"})

    payload = request.get_json(silent=True) or {}
    prompt = payload.get("prompt", "")

    if not prompt:
        svc_errors.add(1, {"service": SERVICE_NAME, "endpoint": "/work", "error.type": "validation"})
        return jsonify({"error": "Missing 'prompt'"}), 400

    # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
    # FIX: Normalize header keys to LOWERCASE
    # The W3C TraceContext propagator expects lowercase 'traceparent'
    # but Flask capitalizes headers (e.g., 'Traceparent')
    # >>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>
    carrier = {key.lower(): value for key, value in request.headers}
    
    # DEBUG: Print incoming headers
    print(f"[WORKER] Traceparent header: {carrier.get('traceparent')}")
    
    ctx = extract(carrier)
    
    # DEBUG: Check if context is valid
    from opentelemetry.trace import get_current_span
    span_ctx = get_current_span(ctx).get_span_context()
    print(f"[WORKER] Extracted context valid: {span_ctx.is_valid}")
    if span_ctx.is_valid:
        print(f"[WORKER] Trace ID: {span_ctx.trace_id:032x}")
    # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<

    with tracer.start_as_current_span("POST /work", context=ctx, kind=SpanKind.SERVER) as span:
        span.set_attribute("http.route", "/work")
        span.set_attribute("http.request.method", "POST")

        with tracer.start_as_current_span("llm.chat.completions", kind=SpanKind.CLIENT) as llm_span:
            llm_requests.add(1, {"provider": LLM_PROVIDER, "operation": "chat"})
            llm_span.set_attribute("gen_ai.request.max_tokens", 100)
            llm_span.set_attribute("gen_ai.request.temperature", 0.7)

            if CAPTURE_PROMPTS:
                llm_span.set_attribute("gen_ai.prompt.0.content", prompt)

            try:
                if LLM_PROVIDER == "openai":
                    llm_span.set_attribute("gen_ai.system", "openai")
                    result, model, dur_ms, err = call_openai(prompt)
                elif LLM_PROVIDER == "claude":
                    llm_span.set_attribute("gen_ai.system", "anthropic")
                    result, model, dur_ms, err = call_claude(prompt)
                else:
                    raise ValueError(f"Unsupported LLM_PROVIDER: {LLM_PROVIDER}")

                llm_span.set_attribute("gen_ai.request.model", model)
                llm_latency.record(dur_ms, {"provider": LLM_PROVIDER, "model": model})

                if err:
                    llm_span.set_status(Status(StatusCode.ERROR))
                    llm_errors.add(1, {"provider": LLM_PROVIDER, "model": model})
                    return jsonify({"error": "LLM call failed", "details": err}), 502

                llm_span.set_attribute("gen_ai.usage.total_tokens", result["total_tokens"])
                if CAPTURE_COMPLETIONS:
                    llm_span.set_attribute("gen_ai.completion.0.content", result["completion"])

                llm_tokens_used.add(result["total_tokens"], {"provider": LLM_PROVIDER, "model": model})

                return jsonify({
                    "provider": result["provider"],
                    "model": result["model"],
                    "completion": result["completion"],
                    "usage": {
                        "prompt_tokens": result["prompt_tokens"],
                        "completion_tokens": result["completion_tokens"],
                        "total_tokens": result["total_tokens"],
                    },
                    "latency_ms": dur_ms,
                }), 200

            except Exception as e:
                llm_span.record_exception(e)
                llm_span.set_status(Status(StatusCode.ERROR))
                llm_errors.add(1, {"provider": LLM_PROVIDER, "model": "unknown"})
                return jsonify({"error": str(e)}), 500
            finally:
                svc_latency.record(int((time.time() - svc_start) * 1000), {"service": SERVICE_NAME, "endpoint": "/work"})


if __name__ == "__main__":
    print("Worker running on http://127.0.0.1:5002/work")
    app.run(host="127.0.0.1", port=5002, debug=False)
