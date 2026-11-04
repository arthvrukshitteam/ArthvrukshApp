"""Point-in-time historical backtest of the AV FQS Monthly SIP model.

The model itself is never reimplemented here — `model_adapter` calls
`backend/fqs_engine.py` directly. This package only controls *what data the
model is allowed to see* at each historical cutoff, and measures what actually
happened afterwards.

See MODEL_AUDIT.md for the audited specification this engine depends on.
"""
