# Vera Message Engine

Deterministic decision engine for the Magicpin AI Challenge. Composes merchant-facing and customer-facing WhatsApp messages from 4 context layers: category, merchant, trigger, and optional customer.

## Architecture

```
INPUT
  → Normalize / validate context
  → Extract candidate signals
  → Filter unsupported signals
  → Score signals (urgency + actionability + fit − penalties)
  → Choose ONE primary signal
  → Compose message from template
  → Validate output
  → Return response
```

Decision first, writing second. No LLM in the request path. Outputs are deterministic for identical input + state.

## API

- `GET /v1/healthz` — liveness + context counts
- `GET /v1/metadata` — bot identity
- `POST /v1/context` — idempotent context push, versioned
- `POST /v1/tick` — judge wakes bot; returns proactive actions
- `POST /v1/reply` — judge delivers merchant/customer reply; bot responds

## Local setup

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Testing

```bash
# Start server first, then:
python test_engine.py

# Generate expanded dataset:
python dataset/generate_dataset.py --seed-dir dataset --out expanded

# Judge simulator (requires an LLM API key):
export BOT_URL=http://localhost:8080
python judge_simulator.py
```

## Deployment

Deploy anywhere exposing `http://<host>:8080/v1/*`. Single-process FastAPI + uvicorn is sufficient.
