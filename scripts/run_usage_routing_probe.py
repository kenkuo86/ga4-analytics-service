#!/usr/bin/env python3
"""Network layer for the Phase 11 usage-routing delivery probe.

Every decision comes from probe_usage_routing.py; this file only moves bytes and
saves evidence. It never reads or writes IAM policy, never deploys, never enables
real emission, and never prints a credential. Sinks enabled for a run are always
disabled again in a finally block, and the exit status reports whether that held.

Requires explicit --apply. Writes are production-shaped: one entry per
entries:write call, exactly as the runtime LoggingWriter sends them.
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_usage_routing as probe

LOGGING = 'https://logging.googleapis.com/v2'
MONITORING = 'https://monitoring.googleapis.com/v3'
IAM_CREDENTIALS = 'https://iamcredentials.googleapis.com/v1'
TOKEN_LIFETIME_SECONDS = 300


class Cloud:
    """Owner credentials are refreshed on a timer and on 401.

    A readiness-gated run outlives a single access token, and an expired one turns
    every write into a silent 401 and leaves the sinks enabled when the cleanup
    path fails too. Neither may be left to chance.
    """

    TOKEN_REFRESH_SECONDS = 600

    def __init__(self, output_dir):
        self.output = output_dir
        self.session = requests.Session()
        self.token_acquired = 0.0
        self.probe_headers = None
        self._refresh_owner_token()

    def _refresh_owner_token(self):
        token = subprocess.check_output(['gcloud', 'auth', 'print-access-token'], text=True).strip()
        self.session.headers['Authorization'] = 'Bearer ' + token
        self.token_acquired = time.monotonic()

    def _fresh_owner_auth(self):
        if time.monotonic() - self.token_acquired >= self.TOKEN_REFRESH_SECONDS:
            self._refresh_owner_token()

    def call(self, method, url, body=None, params=None, headers=None):
        """Reads and control calls retry transient transport faults.

        A run spans tens of minutes, so a dropped connection is expected and must not
        end it. Probe writes deliberately do NOT retry: a resend could be deduplicated
        or counted twice, and either would corrupt the delivery measurement.
        """
        self._fresh_owner_auth()
        for attempt in range(3):
            try:
                response = self.session.request(method, url, json=body, params=params,
                                                headers=headers, timeout=60)
            except requests.exceptions.RequestException as error:
                if attempt == 2:
                    raise RuntimeError(f'{method} {url}: transport failed: {error}') from error
                time.sleep(2 ** attempt)
                continue
            if response.status_code == 401 and headers is None and attempt < 2:
                self._refresh_owner_token()
                continue
            if not response.ok:
                detail = (response.json().get('error', {}).get('message', '')
                          if response.content else '')
                raise RuntimeError(f'{method} {url}: {response.status_code} {detail}')
            return response.json() if response.content else {}
        raise RuntimeError(f'{method} {url}: exhausted retries')

    def save(self, name, data):
        (self.output / f'{name}.json').write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def impersonate(self, service_account):
        """Mint a short-lived token for writes only. Never logged, never persisted."""
        url = f'{IAM_CREDENTIALS}/projects/-/serviceAccounts/{service_account}:generateAccessToken'
        granted = self.call('POST', url, {
            'scope': ['https://www.googleapis.com/auth/logging.write'],
            'lifetime': f'{TOKEN_LIFETIME_SECONDS}s'})
        self.probe_headers = {'Authorization': 'Bearer ' + granted['accessToken']}
        self.save('probe-impersonation', {'service_account': service_account,
                                          'expire_time': granted.get('expireTime'),
                                          'scope': 'logging.write', 'token_recorded': False})
        print(f'Minted {TOKEN_LIFETIME_SECONDS}s write-only token; expires {granted.get("expireTime")}',
              flush=True)

    def toggle_sinks(self, enabled):
        states = {}
        for name in probe.SINKS:
            result = self.call('PATCH', f'{LOGGING}/projects/{probe.PROJECT}/sinks/{name}',
                               {'disabled': not enabled},
                               params={'updateMask': 'disabled', 'uniqueWriterIdentity': 'true'})
            states[name] = result.get('disabled', False)
        self.save('probe-sink-state-' + ('enabled' if enabled else 'disabled'), states)
        print(('Enabled' if enabled else 'Disabled') + ' four POC sinks', flush=True)
        return states

    def confirm_sinks_enabled(self):
        for name in probe.SINKS:
            live = self.call('GET', f'{LOGGING}/projects/{probe.PROJECT}/sinks/{name}')
            if live.get('disabled', False):
                raise RuntimeError(f'sink {name} is disabled; refusing to measure delivery')

    def write_probe(self, record, run_id):
        """One attempt only, by design.

        A resend after an uncertain outcome could be deduplicated by insertId or land
        twice, and either would corrupt the delivery measurement. A transport failure
        is therefore recorded as a rejected write and excluded from the denominator,
        which keeps a dropped connection from ending a run of several hundred probes.
        An impersonated token is never refreshed here: its short life is the point.
        """
        if self.probe_headers is None:
            self._fresh_owner_auth()
        body = probe.write_body(record, run_id)
        ack = {'interaction_id': record['interaction_id'], 'event_name': record['event_name'],
               'insert_id': body['entries'][0]['insertId']}
        try:
            response = self.session.post(f'{LOGGING}/entries:write', json=body,
                                         headers=self.probe_headers, timeout=30)
        except requests.exceptions.RequestException as error:
            return dict(ack, status=None, ok=False, error=str(error)[:200])
        if response.status_code == 401 and self.probe_headers is None:
            # Recover for the writes that follow without resending this one.
            self._refresh_owner_token()
        return dict(ack, status=response.status_code, ok=response.ok)

    def read_destination(self, bucket, location, log_name, run_id, lower, upper, tag):
        pages = []

        def fetch(request):
            response = self.call('POST', f'{LOGGING}/entries:list', request)
            self.save(f'probe-page-{tag}-{bucket}-{log_name}-{len(pages)}', response)
            pages.append(response)
            return response

        request = {'resourceNames': [f'projects/{probe.PROJECT}/locations/{location}'
                                     f'/buckets/{bucket}/views/_AllLogs'],
                   'filter': probe.delivery_filter(log_name, run_id, lower, upper),
                   'pageSize': 1000, 'orderBy': 'timestamp desc'}
        return probe.collect_pages(fetch, request)

    def query_bigquery(self, sql, label):
        """Dry-run first, then run under a hard byte cap. Reads only the raw export table."""
        url = f'https://bigquery.googleapis.com/bigquery/v2/projects/{probe.PROJECT}/queries'
        common = {'query': sql, 'useLegacySql': False, 'location': probe.LOCATION,
                  'maximumBytesBilled': '100000000'}
        estimate = self.call('POST', url, dict(common, dryRun=True))
        self.save(f'probe-bq-{label}-dryrun', estimate)
        result = self.call('POST', url, common)
        self.save(f'probe-bq-{label}-result', result)
        return probe.bigquery_keys(result.get('rows', [])), result

    def export_metrics(self, lower, upper):
        params = {'filter': 'metric.type="logging.googleapis.com/exports/log_entry_count"',
                  'interval.startTime': lower, 'interval.endTime': upper,
                  'aggregation.alignmentPeriod': '60s', 'aggregation.perSeriesAligner': 'ALIGN_SUM'}
        series = self.call('GET', f'{MONITORING}/projects/{probe.PROJECT}/timeSeries', params=params)
        self.save('probe-export-metrics', series)
        return probe.sum_export_points(series)


def reconcile_bigquery(cloud, records, run_id, lower, upper):
    """The KPI views read BigQuery, and each sink drops independently of the others."""
    expected = probe.expected_bq_keys(records)
    per_table = {}
    for table, _, _ in probe.BQ_TABLES:
        sql = probe.bigquery_check_sql(table, run_id, lower, upper)
        observed, raw = cloud.query_bigquery(sql, table.rsplit('.', 1)[-1])
        per_table[table] = probe.reconcile(expected[table], observed)
        per_table[table]['bytes_billed'] = raw.get('totalBytesBilled')
        if not raw.get('jobComplete', True):
            per_table[table]['job_complete'] = False
    complete = all(result.get('job_complete', True) for result in per_table.values())
    return {'per_table': per_table, 'summary': probe.summarize(per_table, complete),
            'gate': probe.gate_check(per_table)}


def read_buckets(cloud, records, run_id, lower, upper, tag):
    """Exact-key read of the Logging buckets for one set of records."""
    observed = {bucket: [] for bucket, _, _ in probe.DESTINATIONS}
    complete = True
    for bucket, location, _ in probe.DESTINATIONS:
        for log_name in (probe.CANONICAL_LOG, probe.SUMMARY_LOG):
            entries, done = cloud.read_destination(bucket, location, log_name,
                                                   run_id, lower, upper, tag)
            complete = complete and done
            observed[bucket].extend(probe.observed_key(entry) for entry in entries)
    expected = probe.expected_keys(records)
    return ({bucket: probe.reconcile(expected[bucket], observed[bucket])
             for bucket, _, _ in probe.DESTINATIONS}, complete)


def await_routing_ready(cloud, args, fixture, canary_run_id):
    """Prove routing is live with canaries before measuring.

    A fixed settle time is not evidence: entries written while sink configuration is
    still propagating are dropped silently, which biases any loss rate measured
    across that window. Canaries are reported separately and never pooled into the
    measurement sample.
    """
    attempts, written = [], []
    for attempt in range(args.readiness_attempts):
        canary = probe.build_records(fixture, 1)
        lower = min(record['event_time'] for record in canary)
        acks = [cloud.write_probe(record, canary_run_id) for record in canary]
        # Keep the full records: their event_time sets the export-metric window below.
        written.extend(record for record, ack in zip(canary, acks) if ack['ok'])
        if not all(ack['ok'] for ack in acks):
            # A rejected canary says nothing about routing; do not read it as not-ready.
            raise RuntimeError(f'canary write rejected: {[ack["status"] for ack in acks]}')
        for _ in range(args.readiness_polls):
            time.sleep(args.readiness_seconds)
            upper = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            reconciliations, complete = read_buckets(cloud, canary, canary_run_id,
                                                     lower, upper, f'canary{attempt}')
            if complete and probe.readiness_reached(reconciliations):
                attempts.append({'attempt': attempt, 'ready': True, 'acks': acks})
                cloud.save('probe-readiness', attempts)
                print(f'Routing confirmed live on canary attempt {attempt}', flush=True)
                return attempts, written
        attempts.append({'attempt': attempt, 'ready': False, 'acks': acks})
        cloud.save('probe-readiness', attempts)
        print(f'Canary attempt {attempt} not delivered; retrying', flush=True)
    raise RuntimeError('routing never confirmed live; refusing to measure across propagation')


def measure(cloud, args, fixture, run_id, canaries=()):
    # Built here, not at start-up: event_time should sit next to the write, as it does
    # in production, and the measurement window must not reach back over the canaries.
    records = probe.build_records(fixture, args.probes)
    lower = min(record['event_time'] for record in records)
    acks = []
    for index, record in enumerate(records):
        acks.append(cloud.write_probe(record, run_id))
        cloud.save('probe-write-acks', acks)  # rewritten each time so a crash keeps evidence
        if index + 1 < len(records):
            time.sleep(args.spacing_seconds)
    last_write = time.monotonic()
    accepted = sum(1 for ack in acks if ack['ok'])
    print(f'Wrote {len(acks)} entries; {accepted} accepted by the Logging API', flush=True)
    # Only accepted writes can be delivered. Counting rejected ones as expected would
    # report an authentication or quota failure as routing loss.
    delivered_scope = probe.records_from_acks(acks)
    if not delivered_scope:
        raise RuntimeError('no write was accepted; nothing to measure')
    if accepted < len(acks):
        print(f'WARNING: {len(acks) - accepted} writes were rejected and are excluded '
              f'from the delivery denominator', flush=True)

    if args.polls < 1:
        raise ValueError('at least one poll is required to measure delivery')
    result = None
    for poll in range(args.polls):
        time.sleep(args.poll_seconds)
        upper = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        reconciliations, complete = read_buckets(cloud, delivered_scope, run_id, lower, upper, poll)
        result = {'poll': poll, 'window': {'lower': lower, 'upper': upper},
                  'accepted_writes': accepted, 'attempted_writes': len(acks),
                  'per_destination': reconciliations,
                  'summary': probe.summarize(reconciliations, complete)}
        cloud.save('probe-delivery-result', result)
        print(f"poll {poll}: delivered {result['summary']['delivered']}"
              f"/{result['summary']['expected']}"
              f" verdict={result['summary']['verdict']}", flush=True)
        if result['summary']['delivered'] == result['summary']['expected'] and complete:
            break
    metric_lower, metric_upper = probe.export_metric_window(lower, canaries)
    # Canaries were written just before this window and their exports land inside it,
    # so they belong in the expected counter even though they are not measured.
    settled = (time.monotonic() - last_write) >= probe.EXPORT_METRIC_LAG_SECONDS
    result['export_metrics'] = probe.export_cross_check(
        cloud.export_metrics(metric_lower, metric_upper),
        probe.expected_exports(list(delivered_scope) + list(canaries)), settled)
    result['export_metrics_settled'] = settled
    result['bigquery'] = reconcile_bigquery(cloud, delivered_scope, run_id, lower,
                                            result['window']['upper'])
    cloud.save('probe-delivery-result', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--fixture', type=Path)
    parser.add_argument('--probes', type=int, default=50)
    parser.add_argument('--apply', action='store_true', required=True,
                        help='required acknowledgement that this run touches the cloud')
    parser.add_argument('--enable-sinks', action='store_true')
    parser.add_argument('--impersonate-sa')
    parser.add_argument('--spacing-seconds', type=float, default=1.0)
    parser.add_argument('--settle-seconds', type=float, default=180.0)
    parser.add_argument('--poll-seconds', type=float, default=60.0)
    parser.add_argument('--polls', type=int, default=8)
    parser.add_argument('--readiness-attempts', type=int, default=6)
    parser.add_argument('--readiness-polls', type=int, default=4)
    parser.add_argument('--readiness-seconds', type=float, default=30.0)
    parser.add_argument('--skip-readiness', action='store_true',
                        help='measure without proving routing is live; biases the loss rate')
    parser.add_argument('--reconcile-acks', type=Path,
                        help='reconcile BigQuery against a saved ack file; writes nothing new')
    parser.add_argument('--run-id')
    parser.add_argument('--window-lower')
    parser.add_argument('--window-upper')
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    if args.reconcile_acks:
        cloud = Cloud(args.output_dir)
        records = probe.records_from_acks(json.loads(args.reconcile_acks.read_text()))
        outcome = reconcile_bigquery(cloud, records, args.run_id,
                                     args.window_lower, args.window_upper)
        cloud.save('probe-bigquery-reconciliation', outcome)
        print(json.dumps(outcome['summary'], indent=2))
        return 0

    fixture = json.loads(args.fixture.read_text())
    probe.plan_document(args.probes)  # reject an out-of-range sample before touching the cloud
    run_id = args.run_id or str(uuid.uuid4())
    # Canaries carry their own run id so the measurement filter can never see them.
    canary_run_id = str(uuid.uuid4())
    print(f'Probe run id {run_id}; canary run id {canary_run_id}', flush=True)
    cloud = Cloud(args.output_dir)
    enabled_here = False
    try:
        if args.enable_sinks:
            cloud.toggle_sinks(True)
            enabled_here = True
            print(f'Waiting {args.settle_seconds}s for sink configuration to settle', flush=True)
            time.sleep(args.settle_seconds)
        cloud.confirm_sinks_enabled()
        canaries = ()
        if not args.skip_readiness:
            _, canaries = await_routing_ready(cloud, args, fixture, canary_run_id)
        if args.impersonate_sa:
            cloud.impersonate(args.impersonate_sa)
        result = measure(cloud, args, fixture, run_id, canaries)
    finally:
        if enabled_here:
            # Leaving sinks enabled is the one outcome worse than a failed run, so this
            # retries with fresh credentials and still shouts if it cannot confirm.
            for attempt in range(3):
                try:
                    cloud._refresh_owner_token()
                    states = cloud.toggle_sinks(False)
                    if all(states.values()):
                        break
                    print('WARNING: a sink did not report disabled', flush=True)
                except Exception as error:  # noqa: BLE001 — cleanup must not mask itself
                    print(f'Sink disable attempt {attempt} failed: {error}', flush=True)
                    time.sleep(5)
            else:
                print('CRITICAL: could not disable sinks; disable them manually now',
                      flush=True)
    print(json.dumps({'logging_buckets': result['summary'],
                      'bigquery': result['bigquery']['summary'],
                      'canonical_gate': result['bigquery']['gate'],
                      'export_metrics': result['export_metrics']}, indent=2))
    failed = {result['summary']['verdict'], result['bigquery']['summary']['verdict']}
    if 'ISOLATION_FAILED' in failed:
        return 1
    return 0 if result['bigquery']['gate']['status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
