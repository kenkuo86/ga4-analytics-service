#!/usr/bin/env python3
"""Phase 11 usage-routing delivery probe: contract, filters and reconciliation.

Never authenticates or applies changes. `run_usage_routing_probe.py` supplies the
network layer and calls into here for every decision, so the delivery verdict is
unit-testable offline.

Delivery is reported as a measured rate with a Wilson upper bound, not as a
zero-loss assertion: Cloud Logging accepts entries before routing them and does
not guarantee exactly-once delivery.
"""
from __future__ import annotations
import argparse
import json
import math
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT = 'ga4-reports-dev'
LOCATION = 'asia-east1'
CANONICAL_EVENT = 'analytics_request_completed'
SUMMARY_EVENT = 'analytics_request_summary'
CANONICAL_LOG = 'ga4_mcp_test_v1'
SUMMARY_LOG = 'ga4_mcp_test_summary_v1'
SINKS = ('ga4-mcp-test-events-bq-v1', 'ga4-mcp-test-events-bucket-v1',
         'ga4-mcp-test-summary-bq-v1', 'ga4-mcp-test-summary-bucket-v1')
# (bucket, location, expected event) — _Default must stay empty under the exclusion.
DESTINATIONS = (('ga4_mcp_test_events', LOCATION, CANONICAL_EVENT),
                ('ga4_mcp_test_summary', LOCATION, SUMMARY_EVENT),
                ('_Default', 'global', None))
# Each sink exports independently, so the bucket copy is not evidence about the
# BigQuery copy: the KPI views read BigQuery and it must be reconciled on its own.
BQ_TABLES = ((f'{PROJECT}.ga4_mcp_test_events.ga4_mcp_test_v1', CANONICAL_EVENT, CANONICAL_LOG),
             (f'{PROJECT}.ga4_mcp_test_summary.ga4_mcp_test_summary_v1', SUMMARY_EVENT, SUMMARY_LOG))
MAX_PROBES = 400
MAX_PAGES = 30
UUID_PATTERN = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
RFC3339_PATTERN = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})')


def log_name_for(event_name):
    """Mirror LoggingWriter: the attachment is the only entry on the summary log."""
    return SUMMARY_LOG if event_name == SUMMARY_EVENT else CANONICAL_LOG


def insert_id_for(record):
    """Mirror LoggingWriter: stable across retries, distinct per event of one request."""
    return f"{record['interaction_id']}:{record['event_name']}"


def write_body(record, run_id=None, project=PROJECT):
    """One entry per request, exactly as the runtime writer sends it.

    The deliberate differences are both synthetic markers: usage_validation keeps
    probes out of the deduplicated KPI views, and usage_probe_run lets one run be
    selected by a single filter term. Enumerating every interaction id instead
    would approach the Logging filter length limit at a few hundred probes and
    fail exactly when the sample is large enough to matter.
    """
    labels = {'usage_environment': 'pilot', 'usage_schema': '1.0', 'usage_validation': 'true'}
    if run_id is not None:
        if not UUID_PATTERN.fullmatch(run_id):
            raise ValueError('run id must be a lowercase UUID')
        labels['usage_probe_run'] = run_id
    return {'logName': f"projects/{project}/logs/{log_name_for(record['event_name'])}",
            'resource': {'type': 'global', 'labels': {'project_id': project}},
            'labels': labels,
            'entries': [{'timestamp': record['event_time'],
                         'insertId': insert_id_for(record), 'jsonPayload': record}]}


def build_records(fixture, count, now=None, project=PROJECT):
    """Produce `count` independent interactions, each a canonical event plus attachment."""
    if not 1 <= count <= MAX_PROBES:
        raise ValueError(f'count must be between 1 and {MAX_PROBES}')
    moment = now or datetime.now(timezone.utc)
    records = []
    for index in range(count):
        interaction = str(uuid.uuid4())
        stamp = (moment + timedelta(microseconds=index)).isoformat().replace('+00:00', 'Z')
        for key in ('canonical', 'summary'):
            record = dict(fixture[key])
            record.update(interaction_id=interaction, event_time=stamp)
            records.append(record)
    return records


def records_from_acks(acks):
    """Rebuild expected-key inputs from a saved ack file; rejected writes are not expected."""
    return [{'interaction_id': ack['interaction_id'], 'event_name': ack['event_name']}
            for ack in acks if ack.get('ok')]


def entry_key(record, project=PROJECT):
    return (record['interaction_id'], record['event_name'],
            f"projects/{project}/logs/{log_name_for(record['event_name'])}",
            insert_id_for(record))


def expected_keys(records, project=PROJECT):
    """Expected multiset per destination bucket; _Default must receive nothing."""
    expected = {bucket: Counter() for bucket, _, _ in DESTINATIONS}
    for record in records:
        for bucket, _, event in DESTINATIONS:
            if event is not None and record['event_name'] == event:
                expected[bucket][entry_key(record, project)] += 1
    return expected


def observed_key(entry):
    payload = entry.get('jsonPayload') or {}
    return (payload.get('interaction_id'), payload.get('event_name'),
            entry.get('logName'), entry.get('insertId'))


def delivery_filter(log_name, run_id, lower, upper, project=PROJECT):
    """Select one probe run by its label. Constant length whatever the sample size."""
    if log_name not in (CANONICAL_LOG, SUMMARY_LOG):
        raise ValueError('unexpected log name')
    if not UUID_PATTERN.fullmatch(run_id or ''):
        raise ValueError('run id must be a lowercase UUID')
    for bound in (lower, upper):
        if not RFC3339_PATTERN.fullmatch(bound):
            raise ValueError('timestamp bounds must be RFC3339')
    return (f'logName="projects/{project}/logs/{log_name}"'
            f' AND timestamp>="{lower}" AND timestamp<="{upper}"'
            f' AND labels.usage_validation="true" AND labels.usage_probe_run="{run_id}"')


def bigquery_check_sql(table, run_id, lower, upper):
    """Exact-key read of one raw export table. Partition filter is mandatory there."""
    if table not in {name for name, _, _ in BQ_TABLES}:
        raise ValueError('unexpected table')
    if not UUID_PATTERN.fullmatch(run_id or ''):
        raise ValueError('run id must be a lowercase UUID')
    for bound in (lower, upper):
        if not RFC3339_PATTERN.fullmatch(bound):
            raise ValueError('timestamp bounds must be RFC3339')
    return (f"SELECT JSON_VALUE(TO_JSON(jsonPayload), '$.interaction_id') AS interaction_id,\n"
            f"       JSON_VALUE(TO_JSON(jsonPayload), '$.event_name') AS event_name,\n"
            '       logName, insertId\n'
            f'FROM `{table}`\n'
            f"WHERE timestamp >= TIMESTAMP('{lower}') AND timestamp <= TIMESTAMP('{upper}')\n"
            f"  AND JSON_VALUE(TO_JSON(labels), '$.usage_probe_run') = '{run_id}'")


def bigquery_keys(rows):
    """Rows come back as jobs.query `f`/`v` cells in the SELECT order above."""
    keys = []
    for row in rows:
        cells = [cell.get('v') for cell in row.get('f', [])]
        if len(cells) != 4:
            raise ValueError('unexpected row shape from BigQuery')
        keys.append(tuple(cells))
    return keys


def expected_bq_keys(records, project=PROJECT):
    """Expected multiset per raw export table, keyed exactly as the bucket check is."""
    expected = {table: Counter() for table, _, _ in BQ_TABLES}
    for record in records:
        for table, event, _ in BQ_TABLES:
            if record['event_name'] == event:
                expected[table][entry_key(record, project)] += 1
    return expected


def collect_pages(fetch, request, max_pages=MAX_PAGES):
    """Read every page. Returns (entries, complete); complete=False must never read as zero."""
    entries, current = [], dict(request)
    for _ in range(max_pages):
        response = fetch(current)
        entries.extend(response.get('entries', []))
        token = response.get('nextPageToken')
        if not token:
            return entries, True
        current = dict(current, pageToken=token)
    return entries, False


def reconcile(expected, observed):
    """Compare exact (interaction_id, event_name, logName, insertId) multisets."""
    wanted, seen = Counter(expected), Counter(observed)
    delivered, missing, unexpected = wanted & seen, wanted - seen, seen - wanted
    total = sum(wanted.values())
    return {'expected': total, 'delivered': sum(delivered.values()),
            'missing': sorted(missing.elements()), 'unexpected': sorted(unexpected.elements()),
            'delivery_rate': sum(delivered.values()) / total if total else None,
            'loss_rate': sum(missing.values()) / total if total else None,
            'isolation_holds': not unexpected}


def wilson_interval(successes, trials, z=1.959963984540054):
    """Two-sided Wilson score interval; with 0 losses it still yields a non-zero bound."""
    if trials <= 0:
        return (None, None)
    proportion = successes / trials
    denominator = 1 + z * z / trials
    centre = (proportion + z * z / (2 * trials)) / denominator
    half = z * math.sqrt(proportion * (1 - proportion) / trials
                         + z * z / (4 * trials * trials)) / denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def summarize(reconciliations, searches_complete=True):
    """Aggregate routed destinations only; _Default contributes isolation, not loss."""
    routed = {name: result for name, result in reconciliations.items() if result['expected']}
    expected = sum(result['expected'] for result in routed.values())
    delivered = sum(result['delivered'] for result in routed.values())
    lost = expected - delivered
    low, high = wilson_interval(lost, expected)
    return {'searches_complete': searches_complete,
            'expected': expected, 'delivered': delivered, 'lost': lost,
            'delivery_rate': delivered / expected if expected else None,
            'loss_rate': lost / expected if expected else None,
            'loss_rate_95ci': None if low is None else [low, high],
            'isolation_holds': all(result['isolation_holds'] for result in reconciliations.values()),
            'verdict': verdict(reconciliations, searches_complete)}


def verdict(reconciliations, searches_complete):
    if not searches_complete:
        return 'INCOMPLETE'
    if not all(result['isolation_holds'] for result in reconciliations.values()):
        return 'ISOLATION_FAILED'
    return 'MEASURED'


def sum_export_points(series, sink_names=SINKS):
    """Total exports/log_entry_count per sink, or None when the sink has no series.

    Monitoring ingests these counters minutes behind the writes, so an absent series
    means "not readable yet", never "nothing was exported". Reporting it as 0 would
    repeat the pagination mistake of treating an incomplete read as a zero result.
    """
    totals = {}
    for stream in series.get('timeSeries', []):
        name = stream.get('resource', {}).get('labels', {}).get('name')
        if name not in sink_names:
            continue
        totals[name] = totals.get(name, 0) + sum(
            int(point.get('value', {}).get('int64Value')
                or point.get('value', {}).get('doubleValue') or 0)
            for point in stream.get('points', []))
    return {name: totals.get(name) for name in sink_names}


EXPORT_METRIC_LAG_SECONDS = 900


def export_metric_window(measurement_lower, canaries=(), margin_minutes=2, now=None):
    """Window covering every write whose export is counted as expected.

    Readiness may retry for several minutes, so the window has to reach back past the
    canaries. Counting a canary as expected while its export sits outside the window
    would manufacture a shortfall — the mirror image of omitting it entirely.
    """
    stamps = [measurement_lower] + [record['event_time'] for record in canaries]
    for stamp in stamps:
        if not RFC3339_PATTERN.fullmatch(stamp):
            raise ValueError('timestamps must be RFC3339')
    earliest = datetime.fromisoformat(min(stamps).replace('Z', '+00:00'))
    end = now or datetime.now(timezone.utc)
    return ((earliest - timedelta(minutes=margin_minutes)).isoformat().replace('+00:00', 'Z'),
            (end + timedelta(minutes=1)).isoformat().replace('+00:00', 'Z'))


def export_cross_check(observed, expected, settled=True):
    """Compare sink counters with the expected export volume, per sink.

    These counters trail the writes by many minutes and converge slowly, so a count
    below expectation is only evidence of a shortfall once the window has settled.
    Before that it is reported as inconclusive: this check corroborates the exact-key
    reconciliation against the buckets and BigQuery, and must never override it.
    """
    status = {}
    for name, wanted in expected.items():
        seen = observed.get(name)
        if seen is None:
            status[name] = {'expected': wanted, 'observed': None, 'status': 'unavailable'}
            continue
        if seen == wanted:
            verdict_name = 'match'
        elif seen > wanted:
            verdict_name = 'excess'
        else:
            verdict_name = 'short' if settled else 'inconclusive'
        status[name] = {'expected': wanted, 'observed': seen, 'status': verdict_name}
    return status


def expected_exports(records):
    """Each routed entry should be exported once per matching sink (bucket + BigQuery)."""
    canonical = sum(1 for record in records if record['event_name'] == CANONICAL_EVENT)
    summary = sum(1 for record in records if record['event_name'] == SUMMARY_EVENT)
    return {'ga4-mcp-test-events-bq-v1': canonical, 'ga4-mcp-test-events-bucket-v1': canonical,
            'ga4-mcp-test-summary-bq-v1': summary, 'ga4-mcp-test-summary-bucket-v1': summary}


CANONICAL_BQ_TABLE = BQ_TABLES[0][0]
CANONICAL_DELIVERY_GATE = 0.95


def gate_check(per_table, minimum=CANONICAL_DELIVERY_GATE):
    """Evaluate the rollout gate on canonical delivery to BigQuery, the KPI source.

    Judged on the Wilson lower bound, not the point estimate: a small sample that
    happens to look clean is not proof the gate is met. The summary attachment is
    deliberately excluded — it carries no count that a KPI depends on.
    """
    result = per_table.get(CANONICAL_BQ_TABLE)
    if not result or not result['expected']:
        return {'gate': minimum, 'status': 'NOT_MEASURED'}
    low, _ = wilson_interval(result['delivered'], result['expected'])
    return {'gate': minimum, 'source': CANONICAL_BQ_TABLE,
            'expected': result['expected'], 'delivered': result['delivered'],
            'delivery_rate': result['delivery_rate'],
            'delivery_rate_95_lower': low,
            'status': 'PASS' if low is not None and low >= minimum else 'FAIL'}


def readiness_reached(reconciliations):
    """A canary counts as proof only when every routed destination has its exact keys."""
    routed = [result for result in reconciliations.values() if result['expected']]
    return bool(routed) and all(result['delivered'] == result['expected'] for result in routed) \
        and all(result['isolation_holds'] for result in reconciliations.values())


def plan_document(count):
    if not 1 <= count <= MAX_PROBES:
        raise ValueError(f'count must be between 1 and {MAX_PROBES}')
    return {'mode': 'PLAN_ONLY', 'project': PROJECT, 'location': LOCATION,
            'probes': count, 'entries': count * 2, 'sinks': list(SINKS),
            'writes': 'one entry per entries:write call, matching the runtime writer',
            'synthetic_marker': 'labels.usage_validation="true"',
            'reads': 'entries.list with full pagination; Monitoring exports/log_entry_count',
            'never': ['IAM reads or writes', 'deployment', 'real emission',
                      'credential output', 'leaving sinks enabled'],
            'acceptance': 'measured delivery rate with Wilson bound; zero loss is not asserted'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--probes', type=int, default=50)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    document = plan_document(args.probes)
    (args.output_dir / 'usage-routing-probe-plan.json').write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + '\n')
    print('Plan only: wrote probe manifest; no cloud authentication or mutation.')


if __name__ == '__main__':
    main()
