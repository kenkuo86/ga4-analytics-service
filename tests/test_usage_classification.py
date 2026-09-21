from datetime import date
import unittest
import subprocess
import sys

from capability_registry import capability_registry
from period_contract import resolve_period_intent
from semantic_catalog import semantic_catalog
from traffic_summary_report import TRAFFIC_METRICS
from usage_classification import classify, explicit_period, preflight_period, sanitize_summary, summary_text


class ClassificationTests(unittest.TestCase):
    def test_traffic_contract_does_not_infer_goal_or_relative_period(self):
        for start, end, days in ((date(2026,9,7),date(2026,9,13),7), (date(2026,1,1),date(2026,3,31),90)):
            result = classify('traffic_summary', parsed_start=start, parsed_end=end)
            self.assertEqual(result['metrics'], [m['metric_id'] for m in TRAFFIC_METRICS])
            self.assertEqual(result['dimensions'], ['session_date'])
            self.assertEqual(result['analysis_goal'], 'unknown')
            self.assertEqual(result['period_type'], 'explicit_range')
            self.assertEqual(result['requested_days'], days)
            self.assertEqual(result['comparison_type'], 'previous_period')
            self.assertEqual(result['intent_source'], 'server_rule')

    def test_period_count_uses_existing_union_not_implicit_scan_days(self):
        requests = [
            ('2026-09-01 至 2026-09-01',1,'explicit_range'),
            ('2026-09-01 至 2026-09-07 與 2026-09-05 至 2026-09-10',10,'multiple_periods'),
            ('上週',7,'relative_window'),
            ('過去 120 天',120,'relative_window'),
        ]
        for request, days, kind in requests:
            period = resolve_period_intent(request, today=date(2026,9,18), include_previous_comparison=True)
            result = preflight_period(period.as_dict())
            self.assertEqual(result['requested_days'], days)
            self.assertEqual(result['period_type'], kind)
        for raw in (None, {}, {'outcome':'resolved','requested_days':0,'explicit_periods':[]}, {'outcome':'invalid_period','requested_days':7}):
            self.assertIsNone(preflight_period(raw)['requested_days'])
        self.assertIsNone(explicit_period(date(2026,9,2),date(2026,9,1))['requested_days'])
        self.assertEqual(explicit_period(date(2026,1,1),date(2026,12,31))['requested_days'],365)

    def test_catalog_ids_and_dimensions_only_from_resolved_profile(self):
        for profile in semantic_catalog.profiles:
            result = classify('query_ga4', resolved_metric_ids=['total_users','fake_metric','total_users'], resolved_profile=profile,
                              parsed_start=date(2026,9,1), parsed_end=date(2026,9,7))
            metric = semantic_catalog.get_metric(profile,'total_users')
            self.assertEqual(result['metrics'], ['total_users'])
            self.assertEqual(result['dimensions'], metric['dimensions'])
            self.assertEqual(result['requested_days'],7)
        result = classify('query_ga4', resolved_metric_ids=['total_users'], resolved_profile=None)
        self.assertEqual(result['metrics'], [])

    def test_all_available_metric_does_not_expand_requested_days(self):
        profile = 'ecommerce'
        metric_id = next(key for key, item in semantic_catalog.profiles[profile]['metrics'].items()
                         if item['status'] == 'published' and '@start_date' not in item['sql_template'])
        result = classify('query_ga4', resolved_metric_ids=[metric_id], resolved_profile=profile,
                          parsed_start=date(2026,9,1), parsed_end=date(2026,9,7))
        self.assertEqual(result['requested_days'],7)
        self.assertEqual(result['metrics'],[metric_id])

    def test_preflight_candidates_not_counted_as_executed_metrics(self):
        cap = capability_registry.resolve('GA4 上週流量摘要',today=date(2026,9,18))
        result = classify('get_ga4_capabilities',capability=cap)
        self.assertEqual(result['metrics'], [])
        self.assertEqual(result['dimensions'], [])
        self.assertEqual(result['analysis_goal'],'unknown')
        self.assertEqual(result['requested_days'],7)
        self.assertNotIn('request', result)
        self.assertNotIn('phrase', str(result))

    def test_external_unsupported_keeps_cross_source_and_hint_provenance(self):
        cap = capability_registry.resolve('比較 GA4 與 Meta 廣告花費')
        self.assertEqual(cap['resolution'],'unsupported')
        result = classify('get_ga4_capabilities', capability=cap, goal_hint='comparison', subject_hint='traffic')
        self.assertEqual(result['analysis_subject'],'cross_source')
        self.assertEqual(result['analysis_goal'],'comparison')
        self.assertEqual(result['intent_source'],'host_model_hint')

    def test_hints_invalid_conflicting_or_unproven(self):
        for hint in ({'raw':'text'},'secret email@example.test',None,123):
            result = classify('traffic_summary',goal_hint=hint,subject_hint='cross_source')
            self.assertEqual(result['analysis_goal'],'unknown')
            self.assertEqual(result['analysis_subject'],'traffic')
            self.assertEqual(result['intent_source'],'server_rule')
        for profile in ([], {}, None):
            self.assertEqual(classify('query_ga4',resolved_profile=profile)['metrics'],[])
        self.assertEqual(classify('query_ga4',subject_hint='cross_source')['analysis_subject'],'unknown')
        result = classify('query_ga4',goal_hint='diagnosis')
        self.assertEqual(result['analysis_goal'],'diagnosis')
        self.assertEqual(result['intent_source'],'host_model_hint')
        self.assertEqual(classify('customer_lookup')['intent_source'],'unknown')


class SummaryTests(unittest.TestCase):
    def test_redaction_boundaries_and_controlled_vocabulary(self):
        self.assertEqual(sanitize_summary('查詢 GA4 2026-09-07 至 2026-09-13 的流量摘要'), '查詢 GA4 2026-09-07 至 2026-09-13 的流量摘要')
        self.assertEqual(sanitize_summary('GA4 owner@example.test 0912-345-678'), 'GA4 [redacted] [redacted]')
        for raw in ('Authorization: Bearer secret','cookie=session','access_token=secret','api_key=secret',
                    '-----BEGIN PRIVATE KEY-----','SELECT * FROM private','GA4 王小明 的薪資',
                    '{"request": "GA4", "result": [1,2]}','GA4 https://private.example','GA4 eyJabc.def.ghi',
                    'GA4 👨‍👩‍👧',None,{'secret':'value'},'x'*8193):
            self.assertIsNone(sanitize_summary(raw))
        self.assertEqual(len(sanitize_summary('查詢'*300)),500)
        for raw in ('users0912345678','sessions2125550123','users123.456.789'):
            self.assertIsNone(sanitize_summary(raw))
        for phone in ('0912.345.678','212.555.0123','0912/345/678','+886 (912) 345-678'):
            self.assertEqual(sanitize_summary('GA4 '+phone),'GA4 [redacted]')

    def test_adversarial_summary_finishes_within_subprocess_deadline(self):
        subprocess.run([sys.executable, '-c',
            "from usage_classification import sanitize_summary; "
            "assert sanitize_summary('1234567.'*4+'X') is None; "
            "assert sanitize_summary('1234567.'*1000+'X') is None; "
            "assert sanitize_summary('查詢'*4000+'X') is None"], check=True, timeout=3)


    def test_source_and_unsafe_summary_never_fall_back_to_raw(self):
        classification = classify('traffic_summary',parsed_start=date(2026,9,1),parsed_end=date(2026,9,7))
        self.assertEqual(summary_text(None, source='unavailable', tool_name='traffic_summary', classification=classification),
                         ('查詢 GA4 流量摘要，需求期間 7 天','server_generated'))
        self.assertEqual(summary_text('password secret',source='host_model_generated',tool_name='traffic_summary',classification=classification),
                         (None,'unavailable'))
        self.assertEqual(summary_text('GA4 流量',source='forged',tool_name='traffic_summary',classification=classification),
                         (None,'unavailable'))
        self.assertEqual(summary_text('GA4 流量',source='host_model_generated',tool_name='traffic_summary',classification=classification),
                         ('GA4 流量','host_model_generated'))
