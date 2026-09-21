#!/usr/bin/env python3
"""Render a reviewable Phase 11 cloud plan. Never authenticate or apply changes."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re

PROJECT = 'ga4-reports-dev'
LOCATION = 'asia-east1'
RUNTIME_SA = f'ga4-analytics-service@{PROJECT}.iam.gserviceaccount.com'
RETENTIONS = {'events': 180, 'summary': 30}


def log_filter(kind):
    suffix = 'summary_' if kind == 'summary' else ''
    event = 'analytics_request_summary' if kind == 'summary' else 'analytics_request_completed'
    return (f'logName="projects/{PROJECT}/logs/ga4_mcp_test_{suffix}v1"\n'
            'resource.type="global"\n'
            f'resource.labels.project_id="{PROJECT}"\n'
            'labels.usage_environment="pilot"\nlabels.usage_schema="1.0"\n'
            f'jsonPayload.schema_version="1.0"\njsonPayload.event_name="{event}"')


def plan(owner):
    if not re.fullmatch(r'user:[A-Za-z0-9._+\-]+@[A-Za-z0-9.\-]+', owner):
        raise ValueError('owner must be a user:email principal')
    result = {'project':PROJECT,'location':LOCATION,'mode':'PLAN_ONLY',
              'datasets':[],'logging_buckets':[],'sinks':[],'iam':[],
              'default_sink_exclusion':{
                  'sink':'_Default','name':'ga4-mcp-test-isolated-storage',
                  'filter':f'logName="projects/{PROJECT}/logs/ga4_mcp_test_v1" OR logName="projects/{PROJECT}/logs/ga4_mcp_test_summary_v1"',
                  'disabled':False,
              }}
    for kind,days in RETENTIONS.items():
        dataset=f'ga4_mcp_test_{kind}'
        bucket=dataset
        result['datasets'].append({
            'datasetReference':{'projectId':PROJECT,'datasetId':dataset},'location':LOCATION,
            'description':f'Phase 11 {kind}: active partition TTL {days} days; recovery copies follow platform policy',
            'defaultPartitionExpirationMs':str(days*86400000),
            'defaultTableExpirationMs':str(days*86400000),
            'maxTimeTravelHours':'48',
            # Explicit ACL; do not copy BigQuery's default projectReaders/projectWriters.
            'access':[{'role':'OWNER','userByEmail':owner.removeprefix('user:')}],
            'labels':{'component':'usage-telemetry','schema':'v1'},
        })
        result['logging_buckets'].append({'name':f'projects/{PROJECT}/locations/{LOCATION}/buckets/{bucket}',
                                          'retentionDays':days,'locked':False})
        for target,destination in (
            ('bq',f'bigquery.googleapis.com/projects/{PROJECT}/datasets/{dataset}'),
            ('bucket',f'logging.googleapis.com/projects/{PROJECT}/locations/{LOCATION}/buckets/{bucket}'),
        ):
            sink={'name':f'ga4-mcp-test-{kind}-{target}-v1','destination':destination,
                  'filter':log_filter(kind),'disabled':True}
            if target=='bq':
                sink['bigqueryOptions']={'usePartitionedTables':True}
                result['iam'].append({'resource':f'{PROJECT}.{dataset}','role':'roles/bigquery.dataEditor',
                                      'member_from_sink_writerIdentity':sink['name']})
            result['sinks'].append(sink)
    result['iam'].extend([
        {'resource':f'projects/{PROJECT}','role':'roles/logging.logWriter','member':f'serviceAccount:{RUNTIME_SA}'},
        {'resource':f'projects/{PROJECT}/secrets/ga4-mcp-test-identity-key','role':'roles/secretmanager.secretAccessor',
         'member':f'serviceAccount:{RUNTIME_SA}'},
    ])
    result['secret']={'name':'ga4-mcp-test-identity-key','replication_location':LOCATION,
                      'initial_value':'GENERATE_32_RANDOM_BYTES_BASE64_VIA_STDIN_ONLY',
                      'reuse_existing':True,'rotate_automatically':False}
    result['table_requirements']={
        'partition_field':'timestamp','partition_type':'DAY','requirePartitionFilter':True,
        'raw_tables':['ga4_mcp_test_events.ga4_mcp_test_v1','ga4_mcp_test_summary.ga4_mcp_test_summary_v1'],
        'also_verify':['ga4_mcp_test_events.export_errors','ga4_mcp_test_summary.export_errors'],
        'seed_policy':'SYNTHETIC_ONLY; preserve event_time; labels.usage_validation=true',
    }
    result['enablement']={
        'app_emission':False,'summary_emission':False,'deploy':False,
        'sinks_initially_disabled':True,'enable_for_synthetic_validation_only_after_iam':True,
    }
    result['preconditions']=[
        'Owner approves this resource plan before any cloud mutation.',
        'Owner must separately approve platform recovery retention beyond active TTL before apply.',
        'Re-audit project/folder/organization sinks; no unexpected usage copies.',
        'Preserve all existing _Default exclusions, append only this named exclusion.',
        'On existing datasets/buckets/sinks: compare, report drift, never replace IAM blindly.',
        'Existing inherited administrators and management SAs are an owner-approved exception.',
        'Create buckets and IAM before applying _Default exclusion; do not touch unrelated logs.',
        'Verify exporter-created envelope/schema with synthetic events before creating SQL functions.',
        'Verify raw and export_errors timestamp partitions, TTL, filter and nullable/repeated types.',
        'Do not enable real emission until internal notice, cost/latency/loss gates and pilot are approved.',
    ]
    return result


def dedup_sql(kind, payload_fields):
    days=RETENTIONS[kind]
    dataset=f'{PROJECT}.ga4_mcp_test_{kind}'
    table='ga4_mcp_test_summary_v1' if kind=='summary' else 'ga4_mcp_test_v1'
    event='analytics_request_summary' if kind=='summary' else 'analytics_request_completed'
    projections=[]
    for field in payload_fields:
        name=field['name']
        if not re.fullmatch(r'[a-z_]+',name):
            raise ValueError('unexpected contract field')
        value=f"JSON_VALUE(payload, '$.{name}')"
        if kind=='events' and name=='request_summary':
            value='CAST(NULL AS STRING)'
        elif kind=='events' and name=='request_summary_source':
            value="'unavailable'"
        elif field['mode']=='REPEATED':
            value=f"ARRAY(SELECT JSON_VALUE(item) FROM UNNEST(IFNULL(JSON_QUERY_ARRAY(payload, '$.{name}'), [])) item)"
        elif field['type']=='TIMESTAMP':
            value=f'SAFE_CAST({value} AS TIMESTAMP)'
        elif field['type']=='INT64':
            # Logging exports JSON numbers as FLOAT, so JSON_VALUE can return "7.0".
            # Parse decimal notation, but reject fractional or out-of-range counters.
            decimal=f'SAFE_CAST({value} AS BIGNUMERIC)'
            value=f'SAFE_CAST(IF({decimal} = TRUNC({decimal}), {decimal}, NULL) AS INT64)'
        projections.append(f'  {value} AS {name}')
    return f'''-- Generated from versioned logical payload contract; no raw payload projection or text joins.
-- Requires synthetic ingestion/schema verification before cloud execution.
CREATE OR REPLACE TABLE FUNCTION `{dataset}.deduplicated_v1`(range_start DATE, range_end DATE)
AS (
WITH bounded AS (
  SELECT TO_JSON(jsonPayload) AS payload, receiveTimestamp AS received_at, insertId AS insert_id
  FROM `{dataset}.{table}`
  WHERE timestamp >= TIMESTAMP(range_start, 'Asia/Taipei')
    AND timestamp < TIMESTAMP(DATE_ADD(range_end, INTERVAL 1 DAY), 'Asia/Taipei')
    AND timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
    AND DATE_DIFF(range_end, range_start, DAY) BETWEEN 0 AND {days-1}
    AND COALESCE(JSON_VALUE(TO_JSON(labels), '$.usage_validation'), 'false') != 'true'
), typed AS (
SELECT
{',\n'.join(projections)},
  received_at, insert_id
FROM bounded
)
SELECT * EXCEPT(received_at, insert_id)
FROM typed
WHERE schema_version = '1.0' AND event_name = '{event}'
  AND event_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
  AND event_time <= CURRENT_TIMESTAMP()
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY schema_version, interaction_id, event_name
  ORDER BY received_at DESC, insert_id DESC
) = 1
);
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner-principal',required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    document=plan(args.owner_principal)
    args.output_dir.mkdir(parents=True,exist_ok=True)
    (args.output_dir/'usage-routing-plan.json').write_text(json.dumps(document,ensure_ascii=False,indent=2)+'\n')
    root=Path(__file__).resolve().parents[1]
    for kind,name in (('events','canonical'),('summary','summary')):
        fields=json.loads((root/f'telemetry/{name}.bigquery-payload.v1.json').read_text())
        (args.output_dir/f'{kind}-dedup.sql').write_text(dedup_sql(kind,fields))
    print('Plan only: wrote resource manifest and SQL; no cloud authentication or mutation.')


if __name__=='__main__':
    main()
