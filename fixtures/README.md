# Public development fixtures

Every file here is synthetic, inspectable and intended for implementation tests. None is a hidden evaluation task, model capability measurement, real website citation or proof of learned improvement.

Fixture IDs are readable; production IDs are UUID. source.txt is canonical Unicode text; citation offsets are Python code-point indexes and its SHA-256 is computed on UTF-8 bytes, including the final newline. Tokenization must not change these offsets.

The sample skill is quarantined. Its bundle hash is explicitly synthetic and not a valid deployable bundle manifest. Tests that need a real bundle must package it, compute the real digest and retain provenance; never promote it based on this fixture.
