Hello Team 

This is meant to be a more Technical Thread, related to an AI Observability use case.
Specifically the use case would be about having:
A flow of services not monitored via OneAgent, where spans cross Service A,B, ..., N, heading towards an LLM

In the example:
Service A (Gateway) --- calling ---> Service B (Worker) --- calling ---> External Service OpenAI LLM
1. Gateway creates a new span called POST /work with type CLIENT (because it's making an outgoing call)
2. Before making the HTTP request, it injects trace context into the HTTP headers

More specifically:
1. Gateway receives request → Creates SERVER span
2. Gateway calls Worker → Creates CLIENT span, injects context into headers
3. Worker receives request → Extracts context from headers, creates SERVER span with same Trace ID
4. Worker calls LLM → Creates CLIENT span for the API call
5. All spans close → Timing and attributes recorded
6. Spans exported → Batched and sent to Dynatrace via OTLP
7. Metrics exported → Counters and histograms sent every 5 seconds

While traces tell you about individual requests, metrics tell you about aggregate behavior over time.

The following metrics are captured:
| service.requests | Counter | Total number of requests to each endpoint |
| service.errors | Counter | Total number of errors |
| service.latency.ms | Histogram | How long requests take (distribution) |
| llm.requests | Counter | Total LLM API calls |
| llm.errors | Counter | Failed LLM calls |
| llm.latency.ms | Histogram | LLM response times |

For Dynatrace to properly display connected traces from OTLP data:
- Enable "Service Detection v2 (Settings → Service Detection → Span-based services)
- This tells Dynatrace to create service entities purely from span attributes like `service.name`
- Without this, Dynatrace may show spans but not connect them into a unified service flow view
| llm.tokens.used | Counter | Total tokens consumed |

What we achieve
1. End-to-end visibility — See the complete request flow across services
2. Performance insights— Know exactly where time is spent (Gateway? Worker? LLM?)
3. Error tracking — Trace failures back to their source
4. LLM observability — Track token usage, latency, and costs
5. Aggregate metrics — Monitor trends over time (requests/sec, error rates, p99 latency)

All of this happens installing any Dynatrace agent — just pure OpenTelemetry instrumentation sending data via open standards.


