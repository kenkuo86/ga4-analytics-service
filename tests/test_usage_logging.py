from datetime import datetime, timedelta, timezone
import io
import json
import time
from contextlib import redirect_stdout, redirect_stderr
from queue import Empty
import unittest
from unittest.mock import Mock, patch

from query_policy import QueryPolicyError
from usage_contract import UsageEvent
from usage_identity import AnalyticsContext, bind_context
import usage_logging as usage


def context(transport='mcp'):
    return AnalyticsContext(transport=transport, user_id='a'*64, authenticated=True, authorized=True,
                            authorization_scope_ref='department-active-tenants-v1')


def drain(emitter):
    records = []
    while True:
        try: records.append(emitter.queue.get_nowait())
        except Empty: return records


class LoggingTests(unittest.TestCase):
    def test_writer_distinguishes_attachment_from_event_but_keeps_retry_id(self):
        writer = usage.LoggingWriter('ga4-reports-dev')
        writer.session = Mock()
        event = {'interaction_id':'00000000-0000-4000-8000-000000000001',
                 'event_time':'2026-09-21T00:00:00Z',
                 'event_name':'analytics_request_completed'}
        attachment = dict(event, event_name='analytics_request_summary')
        writer(event)
        writer(attachment)
        writer(event)
        bodies = [call.kwargs['json'] for call in writer.session.post.call_args_list]
        entries = [body['entries'][0] for body in bodies]
        self.assertNotEqual(entries[0]['insertId'], entries[1]['insertId'])
        self.assertEqual(entries[0]['insertId'], entries[2]['insertId'])
        self.assertEqual(entries[0]['timestamp'], entries[1]['timestamp'])
        self.assertTrue(bodies[0]['logName'].endswith('/ga4_mcp_test_v1'))
        self.assertTrue(bodies[1]['logName'].endswith('/ga4_mcp_test_summary_v1'))

    def setUp(self):
        self.emitter = usage.BoundedEmitter(lambda row: None, enabled=True, summaries=True, start_worker=False)
        self.patch = patch.object(usage,'emitter',self.emitter)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_single_terminal_multimetric_and_retries(self):
        for _ in range(2):
            ctx = context()
            with bind_context(ctx):
                usage.begin(ctx,'query_ga4')
                ctx.usage['entered'] = True
                usage.observe_metrics(['total_users','total_sessions'],'ecommerce')
                usage.observe_result({'status':'ok','metrics':[{'row_count':0},{'row_count':3}],
                                      'query_provenance':{'sql':'SECRET SQL'},'rows':['secret result']})
                usage.finish(ctx)
                usage.finish(ctx)
        records = drain(self.emitter)
        canonical = [r for r in records if r['event_name']=='analytics_request_completed']
        self.assertEqual(len(canonical),2)
        self.assertNotEqual(canonical[0]['interaction_id'],canonical[1]['interaction_id'])
        self.assertEqual(canonical[0]['result_row_count'],3)
        self.assertEqual(canonical[0]['metrics'],['total_users','total_sessions'])
        self.assertEqual(canonical[0]['resolution'],'supported')
        self.assertIsNone(canonical[0]['request_summary'])
        self.assertNotIn('SECRET',json.dumps(records))
        self.assertNotIn('secret result',json.dumps(records))

    def test_outcomes_and_preflight_not_activation(self):
        cases = [('tenant_access_denied','denied'),('tenant_confirmation_required','needs_clarification'),
                 ('unsupported_metric','unsupported'),('data_unavailable','failure'),('query_timeout','failure')]
        for code, status in cases:
            ctx = context()
            with bind_context(ctx):
                usage.begin(ctx,'query_ga4'); ctx.usage['entered']=True
                usage.observe_result({'status':code,'message':'token=secret'})
                usage.finish(ctx)
            record=drain(self.emitter)[0]
            self.assertEqual(record['status'],status)
            self.assertEqual(record['error_code'],'timeout' if code=='query_timeout' else code)
        ctx=context()
        with bind_context(ctx):
            usage.begin(ctx,'get_ga4_capabilities',{'request':'GA4 secret raw request'})
            ctx.usage['entered']=True
            usage.observe_result({'status':'ok','resolution':'supported','reason_code':'ga4_traffic_summary','request':'raw secret'})
            usage.finish(ctx)
        record=drain(self.emitter)[0]
        self.assertEqual(record['request_kind'],'capability_preflight')
        self.assertEqual(record['status'],'success')
        self.assertEqual(record['metrics'],[])
        self.assertNotIn('raw secret',json.dumps(record))

    def test_invalid_preflight_date_stays_clarification(self):
        from capability_registry import capability_registry
        ctx=context()
        request='查詢 GA4 2026-02-30 到 2026-03-05 流量摘要'
        with bind_context(ctx):
            usage.begin(ctx,'get_ga4_capabilities',{'request':request});ctx.usage['entered']=True
            usage.observe_result(capability_registry.resolve(request))
            usage.finish(ctx)
        record=drain(self.emitter)[0]
        self.assertEqual(record['status'],'needs_clarification')
        self.assertEqual(record['resolution'],'needs_clarification')
        self.assertEqual(record['error_code'],'invalid_period')
        self.assertIsNone(record['requested_days'])

    def test_policy_denial_retains_reliable_days_not_zero_or_scan_days(self):
        from query_policy import query_policy
        ctx=context()
        with bind_context(ctx):
            usage.begin(ctx,'traffic_summary'); ctx.usage['entered']=True
            try:
                query_policy.validate_date_range('2025-01-01','2025-04-30')
            except QueryPolicyError as error:
                usage.observe_exception(error)
            usage.finish(ctx)
        record=drain(self.emitter)[0]
        self.assertEqual(record['requested_days'],120)
        self.assertEqual(record['status'],'denied')
        self.assertEqual(record['period_type'],'explicit_range')
        self.assertIsNone(record['result_row_count'])

    def test_faults_never_change_success_or_error_and_no_sensitive_fallback(self):
        @usage.observed_handler
        def handler(fail=False):
            if fail: raise QueryPolicyError('data_unavailable','SECRET exception')
            return {'status':'ok','daily_series':[]}
        for target in ('UsageEvent','summary_text'):
            for fail in (True,False):
                stream=io.StringIO()
                ctx=context()
                with bind_context(ctx), patch.object(usage,target,side_effect=RuntimeError('SECRET payload')), redirect_stdout(stream),redirect_stderr(stream):
                    usage.begin(ctx,'traffic_summary')
                    if fail:
                        with self.assertRaises(QueryPolicyError): handler(True)
                    else:
                        self.assertEqual(handler(),{'status':'ok','daily_series':[]})
                    usage.finish(ctx)
                self.assertNotIn('SECRET',stream.getvalue())
        with patch.object(self.emitter,'emit',side_effect=RuntimeError('SECRET logger')):
            ctx=context()
            with bind_context(ctx):
                usage.begin(ctx,'traffic_summary'); handler(); usage.finish(ctx)

    def test_bounded_queue_and_outage_retries_do_not_call_network_in_request(self):
        writer_calls=[]
        def broken(row): writer_calls.append(row); raise TimeoutError('SECRET network')
        emitter=usage.BoundedEmitter(broken,enabled=True,capacity=1,start_worker=False)
        event=UsageEvent(event_time=datetime.now(timezone.utc),interaction_id=context().interaction_id,
                         transport='mcp',request_kind='analytics',status='success',latency_ms=0)
        start=time.monotonic()
        for _ in range(1000): emitter.emit(event)
        self.assertLess(time.monotonic()-start,1)
        self.assertEqual(writer_calls,[])
        self.assertEqual(emitter.queue.qsize(),1)
        emitter.deliver_one(emitter.queue.get_nowait())
        self.assertEqual(len(writer_calls),2)
        bad=event.model_copy(update={'request_summary':'SECRET'})
        emitter.emit(bad)
        self.assertEqual(emitter.queue.qsize(),0)

    def test_summary_retention_and_independent_switch(self):
        ctx=context()
        with bind_context(ctx):
            usage.begin(ctx,'traffic_summary',{'request_summary':'GA4 流量'});ctx.usage['entered']=True
            usage.observe_result({'status':'ok','daily_series':[]});usage.finish(ctx)
        rows=drain(self.emitter)
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['event_time'],rows[1]['event_time'])
        self.assertEqual(rows[0]['interaction_id'],rows[1]['interaction_id'])
        rows[1]['event_time']=(datetime.now(timezone.utc)-timedelta(days=31)).isoformat()
        with patch.object(self.emitter,'writer') as writer:
            self.emitter.deliver_one(rows[1]);writer.assert_not_called()
        self.emitter.summaries=False
        ctx=context()
        with bind_context(ctx):
            usage.begin(ctx,'traffic_summary',{'request_summary':'GA4 流量'});ctx.usage['entered']=True
            usage.observe_result({'status':'ok','daily_series':[]});usage.finish(ctx)
        self.assertEqual(len(drain(self.emitter)),1)


class TransportBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_early_auth_denial_never_reads_body(self):
        writer=usage.BoundedEmitter(lambda row:None,enabled=True,start_worker=False)
        async def receive():
            raise AssertionError('telemetry must not read rejected body')
        async def app(scope,receive,send):
            await send({'type':'http.response.start','status':401,'headers':[]})
            await send({'type':'http.response.body','body':b''})
        async def send(message): pass
        with patch.object(usage,'emitter',writer):
            await usage.UsageTransportMiddleware(app)({'type':'http','path':'/mcp'},receive,send)
        rows=drain(writer)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['request_kind'],'unclassified')
        self.assertEqual(rows[0]['status'],'denied')

    async def test_nested_transport_wrappers_do_not_duplicate(self):
        writer=usage.BoundedEmitter(lambda row:None,enabled=True,start_worker=False)
        async def app(scope,receive,send):
            await send({'type':'http.response.start','status':422,'headers':[]})
        async def noop(*args): pass
        with patch.object(usage,'emitter',writer):
            nested=usage.UsageTransportMiddleware(usage.UsageTransportMiddleware(app))
            await nested({'type':'http','path':'/traffic-summary'},noop,noop)
        self.assertEqual(len(drain(writer)),1)
