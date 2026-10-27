"""Exact, source-bound listing termination evidence for three observed gaps.

The frozen V2 core uses outDate < target. On the first non-listed day it can
therefore demand a status/bar that cannot exist. These official notices resolve
only that ambiguity; unknown missing evidence still holds the entire snapshot.
Raw provider rows, eligible membership, V2 core and G6 core are never edited.
"""
from collections import Counter


TERMINATIONS = {
    '601059.SH': dict(effective_date='20261019', published_date='20261012',
        authority='Shanghai Stock Exchange', notice='2026-123',
        source='https://www.sse.com.cn/disclosure/announcement/listing/stock/c/c_20261012_10834561.shtml'),
    '601198.SH': dict(effective_date='20261019', published_date='20261012',
        authority='Shanghai Stock Exchange', notice='2026-124',
        source='https://www.sse.com.cn/disclosure/announcement/listing/stock/c/c_20261012_10834560.shtml'),
    '000016.SZ': dict(effective_date='20261023', published_date='20261023',
        authority='Issuer filing on CNINFO', notice='2026-103; Shenzhen Stock Exchange decision 2026-1390',
        source='https://static.cninfo.com.cn/finalpage/2026-10-23/1225615607.PDF'),
}


def resolve_official_terminations(snapshot, capture, target):
    """Resolve a missing-status exclusion only with matching exact-date evidence."""
    from datasource.date_effective_universe_v2 import canonical_json_sha256, KNOWN_EXCLUSION_REASONS
    records = []
    expected_before = list(snapshot['expected_codes'])
    status_codes = {r['code'] for r in capture['rows']['all_stock']}
    daily_codes = {r['code'] for r in capture['rows']['daily']}
    for row in snapshot['qualification_failures']:
        symbol = row['canonical_symbol']
        fact = TERMINATIONS.get(symbol)
        if not fact or fact['effective_date'] != target or fact['published_date'] > target:
            continue
        provider_code = symbol[-2:].lower()+'.'+symbol[:6]
        if (row.get('qualification_reason') != 'STATUS_EVIDENCE_MISSING'
                or row.get('out_date') != target or provider_code in status_codes or provider_code in daily_codes
                or symbol in snapshot['expected_codes']):
            continue
        evidence = dict(canonical_symbol=symbol, **fact)
        evidence['evidence_record_sha256'] = canonical_json_sha256(evidence)
        row['previous_qualification_reason'] = row['qualification_reason']
        row['qualification_reason'] = 'DELISTED'
        row['listing_termination_evidence'] = evidence
        for identity in snapshot['positive_mainboard_identity_records']:
            if identity['canonical_symbol'] == symbol:
                identity['date_effective_identity_status'] = 'OFFICIALLY_DELISTED_ON_TRADE_DATE'
                identity['listing_termination_evidence_sha256'] = evidence['evidence_record_sha256']
        records.append(evidence)
    if not records:
        return
    assert snapshot['expected_codes'] == expected_before
    snapshot['authoritative_listing_termination_exclusions'] = records
    snapshot['qualification_failure_counts'] = dict(sorted(Counter(
        r['qualification_reason'] for r in snapshot['qualification_failures']).items()))
    snapshot['qualification_evidence_sha256'] = canonical_json_sha256(snapshot['qualification_failures'])
    unresolved = [r for r in snapshot['qualification_failures']
                  if r['qualification_reason'] not in KNOWN_EXCLUSION_REASONS | {'BOARD_IDENTITY_UNKNOWN'}]
    ready = snapshot['roster_completeness']=='PASS_CANDIDATE' and not unresolved
    snapshot['eligible_completeness'] = 'PASS_CANDIDATE' if ready else 'HOLD'
    snapshot['snapshot_readiness'] = 'READY_CANDIDATE' if ready else 'HOLD'
    snapshot['scope'] += '; exact-date official listing termination exclusions are evidence-bound'
