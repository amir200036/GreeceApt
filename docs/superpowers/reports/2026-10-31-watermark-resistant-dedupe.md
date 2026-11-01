# Watermark-Resistant Dedupe Evaluation

## Historical-positive coverage

- Known positive records: 1224
- Candidate records: 1224
- Isolated policy: 1064 automatic, 53 formerly automatic one/two-photo records demoted
- Isolated final states: HIGH=1064, REVIEW=156, NONE=4
- End-to-end grouped positives: 735
- End-to-end review: 117
- End-to-end none/missing: 372

Demotion reasons:
- `blacklisted_or_no_direct_evidence`: 372
- `insufficient_photo_count`: 102
- `low_photo_overlap`: 15

## Live and safety fixtures

- Frozen live sample: 6 HIGH / 1 REVIEW
- Hard-negative violations: 0 / 6

## Runtime

- Candidate pairs by source: photo=1824, geo=726711, metadata=124766
- Peak candidate pairs: 811028
- 32-image fingerprint time: 0.2708s
- Pair-scoring time: 0.3431s
- Legacy total: 2.9551s
- V2 total: 4.8595s
- V2 / legacy ratio: 1.644
