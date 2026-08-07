# Alpha: A Minimal Study of Fixture Pipelines

## Abstract

We present Alpha, a minimal deterministic study used to exercise the
paper-curation pipeline end to end without external services. The paper
defines one result, one method, and one evaluation table.

## Method

We construct a single synthetic dataset. The pipeline extracts this text,
renders one figure manifest, and publishes a structured Korean review.

## Result

The pipeline produced one review, one HTML page, and one BM25 index entry.
No paid provider was contacted.

## Conclusion

Deterministic fixtures keep end-to-end tests reproducible and free.
