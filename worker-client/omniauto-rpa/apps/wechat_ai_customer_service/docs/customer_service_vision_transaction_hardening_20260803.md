# Vision Current-Image Transaction Hardening

This change follows:

- `customer_visible_reply_ownership_baseline.md`
- `customer_service_external_contract_and_optional_plugin_baseline.md`

## Scope

The optional Vision plugin now owns one strict current-image transaction:

1. observe the complete visible chat surface;
2. re-confirm the host-authorized image slot and sender role;
3. right-click the refreshed slot and locate Copy in the local menu region;
4. require a new, stable clipboard generation containing a bitmap;
5. compare the copied bitmap with the refreshed slot fingerprint, retrying the
   same transaction at most once;
6. clear only the generation proven to belong to the matched image;
7. normalize and compress the image in memory before applying provider limits;
8. treat a schema-valid, non-empty summary as the only completed Vision result.

The fingerprint is supporting copy-consistency evidence. It does not decide
the sender role, select a product, or create customer-visible text.

## Compatibility

The existing `VisionHostPorts` method names and signatures remain unchanged.
Older callers without stable slot evidence retain the frozen compatibility
transaction. Hosts that provide `sender_role`, `image_physical_anchor`, and
`bubble_rect`, or enable `strict_image_adapter`, use the strict transaction.

The PR #28 Sidecar files remain byte-identical. Vision and voice stay separate
optional plugins, and this change adds no scheduler, ledger, Brain, backend, or
product-master ownership to Vision.

## Failure Rules

- More visible image candidates than the observation limit is an explicit
  failure, never silent truncation.
- A moved, ambiguous, or role-conflicting slot is never clicked.
- A stale, changing, non-bitmap, or mismatched clipboard generation never
  reaches the provider.
- A mismatched generation is not cleared because it may belong to the user.
- Failure to clear a matched generation fails closed before provider use.
- Empty or schema-invalid provider output is not a completed result.

## Verification

The regression set includes real production helpers for high-information
1920x1080 compression, non-empty completion, slot re-confirmation, fingerprint
comparison, retry bounds, clipboard cleanup failure, legacy port compatibility,
optional-plugin isolation, PR #28 byte containment, and Win32/OCR compatibility.
