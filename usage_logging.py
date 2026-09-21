"""Failure-isolated usage observation and bounded asynchronous Logging delivery."""
from collections import Counter
from datetime import datetime, timezone
from functools import wraps
import json
import os
from queue import Queue, Empty, Full
from threading import Lock, Thread
import time
from typing import get_args

from pydantic import BeforeValidator, WithJsonSchema
from typing import Annotated, Any

from usage_classification import classify, explicit_period, summary_text
from usage_contract import ErrorCode, Goal, Subject, ToolName, UsageEvent, SummaryAttachment
from usage_identity import AnalyticsContext, current_context

TOOLS = frozenset(get_args(ToolName))
DIAGNOSTICS = Counter()
_DENIED = frozenset({
    'authentication_required','invalid_token','insufficient_scope','tenant_access_denied','tenant_inactive',
    'date_range_too_large','date_before_available_range','future_date_not_allowed',
    'query_cost_limit_exceeded','daily_query_quota_exceeded',
    'invalid_date_format','invalid_date_range','invalid_metric_request','too_many_metrics',
    'invalid_semantic_profile','invalid_period',
})
_CLARIFY = frozenset({'tenant_confirmation_required','customer_name_too_broad','ambiguous_tenant',
                      'semantic_profile_required','tenant_not_found','invalid_customer_name'})
_UNSUPPORTED = frozenset({'unsupported_metric','metric_definition_conflict'})
_EXTERNAL = frozenset({'advertising_data','seo_keyword_ranking','crm','external_data_source'})


def _summary_arg(value):
    return value if isinstance(value, str) and len(value) <= 8192 else None


def _goal_arg(value):
    return value if isinstance(value, str) and value in get_args(Goal) else None


def _subject_arg(value):
    return value if isinstance(value, str) and value in get_args(Subject) else None


SummaryInput = Annotated[Any, BeforeValidator(_summary_arg), WithJsonSchema({'anyOf':[{'type':'string','maxLength':8192},{'type':'null'}]})]
GoalInput = Annotated[Any, BeforeValidator(_goal_arg), WithJsonSchema({'anyOf':[{'enum':list(get_args(Goal))},{'type':'null'}]})]
SubjectInput = Annotated[Any, BeforeValidator(_subject_arg), WithJsonSchema({'anyOf':[{'enum':list(get_args(Subject))},{'type':'null'}]})]


def diagnostic(code):
    # Fixed internal codes only; no exception messages, values, IDs or request text.
    DIAGNOSTICS[code] += 1


def safe_note(fn):
    @wraps(fn)
    def isolated(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            diagnostic('observation_failure')
            return None
    return isolated


@safe_note
def observe_period(start, end):
    context = current_context.get()
    if context is not None and hasattr(context, 'usage'):
        context.usage['period'] = explicit_period(start, end)


@safe_note
def observe_metrics(metric_ids, profile):
    context = current_context.get()
    if context is not None and hasattr(context, 'usage'):
        # Only orchestration after profile resolution may call this hook.
        context.usage['resolved_metric_ids'] = tuple(metric_ids)
        context.usage['resolved_profile'] = profile
        context.usage['resolution'] = 'supported'


@safe_note
def observe_result(result):
    context = current_context.get()
    if context is None or not hasattr(context, 'usage') or not isinstance(result, dict):
        return
    state = context.usage
    code = result.get('status')
    if code == 'query_timeout':
        code = 'timeout'
    resolution = result.get('resolution')
    if code in ('ok', 'customer_found'):
        state['status'] = 'success'
        if state['kind'] == 'analytics':
            state['resolution'] = 'supported'
    elif code in _DENIED:
        state['status'] = 'denied'
    elif code in _CLARIFY:
        state['status'] = 'needs_clarification'
    elif code in _UNSUPPORTED:
        state['status'] = 'unsupported'
        state['resolution'] = 'unsupported'
        state['unsupported_reason'] = code
    else:
        state['status'] = 'failure'
    if code not in ('ok', 'customer_found'):
        state['error_code'] = code if code in get_args(ErrorCode) else 'unknown_error'
    if state['kind'] == 'capability_preflight':
        if resolution in ('supported', 'unsupported', 'needs_clarification'):
            state['resolution'] = resolution
            state['status'] = 'success' if resolution == 'supported' else resolution
        reason = result.get('reason_code')
        if reason in _DENIED:
            state['error_code'] = reason
            if reason != 'invalid_period':
                state['status'] = 'denied'
        if resolution == 'unsupported':
            state['unsupported_reason'] = 'external_source_not_available' if reason in _EXTERNAL else 'unknown'
        # Classifier selects codes/counts. Never retain the capability result (raw request/phrases).
        state['classification'] = classify('get_ga4_capabilities', capability=result,
                                           goal_hint=state.get('goal_hint'),subject_hint=state.get('subject_hint'))
    if state['kind'] == 'analytics' and code == 'ok':
        if state['tool'] == 'traffic_summary':
            rows = result.get('daily_series')
            state['result_row_count'] = len(rows) if isinstance(rows, list) else None
        elif state['tool'] == 'query_ga4':
            metrics = result.get('metrics')
            if isinstance(metrics, list):
                counts = [item.get('row_count') for item in metrics if isinstance(item, dict)]
                if len(counts) == len(metrics) and all(type(n) is int and n >= 0 for n in counts):
                    state['result_row_count'] = sum(counts)


@safe_note
def observe_exception(error):
    detail = getattr(error, 'detail', None)
    if isinstance(detail, dict):
        observe_result(detail)
        return
    code = getattr(error, 'code', None)
    if isinstance(error, TimeoutError):
        code = 'timeout'
    elif code == 'query_timeout':
        code = 'timeout'
    observe_result({'status': code if isinstance(code,str) else 'backend_error'})


def observed_handler(fn):
    """Marks SDK validation success and observes only selected outcome fields."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        context = current_context.get()
        if context is not None and hasattr(context, 'usage'):
            context.usage['entered'] = True
        try:
            result = fn(*args, **kwargs)
        except Exception as error:
            observe_exception(error)
            raise
        observe_result(result)
        return result
    return wrapper


class LoggingWriter:
    """Network runs only in worker. Retry at most once, never in request thread."""
    def __init__(self, project):
        self.project = project
        self.session = None

    def __call__(self, record):
        if self.session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
            credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/logging.write'])
            self.session = AuthorizedSession(credentials, max_refresh_attempts=0)
        log = 'ga4_mcp_test_summary_v1' if record['event_name'] == 'analytics_request_summary' else 'ga4_mcp_test_v1'
        body = {'logName':f'projects/{self.project}/logs/{log}',
                'resource':{'type':'global','labels':{'project_id':self.project}},
                'labels':{'usage_environment':'pilot','usage_schema':'1.0'},
                # Logging deduplicates by project/timestamp/insertId, even across log names.
                # Keep retries stable while distinguishing the canonical event and attachment.
                'entries':[{'timestamp':record['event_time'],
                            'insertId':f"{record['interaction_id']}:{record['event_name']}", 'jsonPayload':record}]}
        response = self.session.post('https://logging.googleapis.com/v2/entries:write', json=body, timeout=2)
        response.raise_for_status()


class BoundedEmitter:
    def __init__(self, writer, *, enabled=False, summaries=False, capacity=256, start_worker=True):
        self.enabled = enabled
        self.summaries = summaries
        self.writer = writer
        self.queue = Queue(maxsize=capacity)
        self.worker = None
        self.start_worker = start_worker

    def emit(self, model):
        if not self.enabled:
            return
        try:
            record = model.wire_dict()
            if record['event_name'] == 'analytics_request_summary' and not self.summaries:
                return
            self.queue.put_nowait(record)
            diagnostic('enqueued')
            if self.start_worker and self.worker is None:
                # Assign before starting so concurrent requests see the same worker.
                # Worker creation is guarded by module lock shared by emitters.
                with _WORKER_LOCK:
                    if self.worker is None:
                        self.worker = Thread(target=self._run, daemon=True, name='usage-logging')
                        self.worker.start()
        except Full:
            diagnostic('queue_full')
        except Exception:
            diagnostic('serialization_failure')

    def deliver_one(self, record):
        try:
            timestamp = datetime.fromisoformat(record['event_time'].replace('Z','+00:00'))
            ttl = 30 if record['event_name'] == 'analytics_request_summary' else 180
            if (datetime.now(timezone.utc) - timestamp).total_seconds() >= ttl * 86400:
                diagnostic('expired_dropped')
                return
            for attempt in range(2):
                try:
                    self.writer(record)
                    diagnostic('delivered')
                    return
                except Exception:
                    diagnostic('delivery_failure')
            diagnostic('delivery_dropped')
        except Exception:
            diagnostic('delivery_dropped')

    def _run(self):
        last_report = time.monotonic()
        while True:
            try:
                record = self.queue.get(timeout=1)
            except Empty:
                record = None
            if record is not None:
                try:
                    self.deliver_one(record)
                finally:
                    self.queue.task_done()
            if time.monotonic() - last_report >= 30:
                try:
                    # Payload-free stdout diagnostic, never usage text or exceptions.
                    print(json.dumps({'severity':'INFO','usage_diagnostics':dict(DIAGNOSTICS), 'queue_depth':self.queue.qsize()}), flush=True)
                except Exception:
                    pass
                last_report = time.monotonic()


_WORKER_LOCK = Lock()
emitter = BoundedEmitter(LoggingWriter('ga4-reports-dev'),
                         enabled=os.getenv('USAGE_ENABLED','false').lower() == 'true',
                         summaries=os.getenv('USAGE_SUMMARY_ENABLED','false').lower() == 'true')


def begin(context, tool, args=None):
    args = args if isinstance(args, dict) else {}
    kind = 'analytics' if tool in ('query_ga4','traffic_summary') else 'discovery' if tool in TOOLS else 'unclassified'
    if tool == 'get_ga4_capabilities' and isinstance(args.get('request'),str) and args['request'].strip():
        kind = 'capability_preflight'
    context.usage = {'tool': tool if tool in TOOLS else None, 'kind':kind, 'entered':False,
                     'status':'failure','error_code':None,'resolution':'unknown'}
    if emitter.summaries:
        context.usage['summary'] = _summary_arg(args.get('request_summary'))
        context.usage['summary_invalid'] = args.get('request_summary') is not None and context.usage['summary'] is None
    context.usage['goal_hint'] = _goal_arg(args.get('analysis_goal_hint'))
    context.usage['subject_hint'] = _subject_arg(args.get('analysis_subject_hint'))


@safe_note
def finish(context):
    if not emitter.enabled or not hasattr(context, 'usage'):
        return
    state = context.usage
    if state.get('finished'):
        return
    state['finished'] = True
    tool = state['tool']
    classified = state.get('classification')
    if classified is None:
        classified = classify(tool, resolved_metric_ids=state.get('resolved_metric_ids',()),
                              resolved_profile=state.get('resolved_profile'),
                              goal_hint=state.get('goal_hint'),subject_hint=state.get('subject_hint'))
        classified.update(state.get('period',{}))
        if not state['entered']:
            classified.update(metrics=[],dimensions=[],analysis_goal='unknown',analysis_subject='unknown',intent_source='unknown',comparison_type='unknown',period_type='unknown',requested_days=None)
    event_time = datetime.now(timezone.utc)
    event = UsageEvent(event_time=event_time,interaction_id=context.interaction_id,transport=context.transport,
                       request_kind=state['kind'], user_id=context.user_id,
                       identity_status='verified' if context.user_id else 'unavailable',host=context.host,
                       tenant_id=context.tenant_id,authorization_scope_ref=context.authorization_scope_ref,
                       authorization_result=context.authorization_result,tool_name=tool,
                       status=state['status'],resolution=state['resolution'],error_code=state.get('error_code'),
                       unsupported_reason=state.get('unsupported_reason'),
                       latency_ms=max(0,int((time.monotonic()-context.started_at)*1000)),
                       result_row_count=state.get('result_row_count'),**classified)
    emitter.emit(event)
    if emitter.summaries and state['entered'] and not state.get('summary_invalid'):
        text, source = summary_text(state.get('summary'), source='host_model_generated' if context.transport=='mcp' else 'user_input',
                                    tool_name=tool,classification=classified)
        if text:
            emitter.emit(SummaryAttachment(event_time=event_time,interaction_id=context.interaction_id,
                                           request_summary=text,request_summary_source=source))
    if context.tenant_id_quality:
        diagnostic(context.tenant_id_quality)


class MCPUsageMiddleware:
    async def __call__(self, ctx, call_next):
        if ctx.method != 'tools/call' or not emitter.enabled:
            return await call_next(ctx)
        context = current_context.get()
        if context is None:
            return await call_next(ctx)
        if ctx.request is not None and ctx.request.scope.get('usage_transport') is not None:
            ctx.request.scope['usage_transport']['rpc_owned'] = True
        try:
            params = ctx.params if isinstance(ctx.params, dict) else {}
            tool = params.get('name')
            begin(context, tool if isinstance(tool,str) else None, params.get('arguments'))
        except Exception:
            diagnostic('observation_failure')
        try:
            result = await call_next(ctx)
            if hasattr(context,'usage') and not context.usage['entered']:
                context.usage['error_code'] = 'invalid_schema' if context.usage['tool'] else 'unknown_tool'
            return result
        except Exception as error:
            observe_exception(error)
            if hasattr(context,'usage') and not context.usage['entered']:
                context.usage['error_code'] = 'invalid_schema' if context.usage['tool'] else 'unknown_tool'
            raise
        finally:
            finish(context)


class UsageTransportMiddleware:
    """Observe early rejection without reading bodies or response payloads."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path')
        if scope['type'] != 'http' or path not in ('/mcp','/mcp/','/traffic-summary') or not emitter.enabled or 'usage_transport' in scope:
            return await self.app(scope,receive,send)
        transport = {'started_at': time.monotonic(), 'context': None, 'rpc_owned': False}
        scope['usage_transport'] = transport
        status = 500
        async def capture(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
            await send(message)
        try:
            return await self.app(scope,receive,capture)
        finally:
            try:
                if not transport['rpc_owned'] and (path == '/traffic-summary' or status >= 400):
                    context = transport['context'] or AnalyticsContext(transport='rest' if path=='/traffic-summary' else 'mcp',started_at=transport['started_at'])
                    if not hasattr(context,'usage'):
                        begin(context,'traffic_summary' if path=='/traffic-summary' else None)
                    if not context.usage['entered']:
                        context.usage['status'] = 'denied' if status in (401,403) else 'failure'
                        context.usage['error_code'] = 'authentication_required' if status==401 else 'insufficient_scope' if status==403 else 'invalid_schema' if status in (400,422) else 'backend_error'
                        if status in (401,403):
                            context.authorization_result = 'denied'
                    finish(context)
            except Exception:
                diagnostic('observation_failure')


def consent_usage_notice():
    if not emitter.enabled:
        return ''
    summary = '經清理的當次需求摘要另存30天。' if emitter.summaries else '目前不保存文字摘要。'
    return ('<section aria-labelledby="usage-title"><h2 id="usage-title">內部產品使用分析</h2>'
            '<p>為改善GA Analytics，服務會記錄匿名化使用者識別、工具、客戶識別、指標、期間、'
            '處理結果與耗時，結構化事件保存180天。' + summary +
            '首次成功使用紀錄的保存上限為量測開始起一年，不因回訪延長。'
            '不保存登入憑證、完整對話、SQL或查詢結果。資料由服務負責人管理，'
            '既有GCP管理員及管理用服務帳戶保留管理存取權。'
            '如需查詢或刪除紀錄，請聯絡服務負責人；這些資料不作員工績效評估。</p></section>')
