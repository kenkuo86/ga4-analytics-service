from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


SERVICE_ORIGIN = "https://ga4-analytics-service-398991472921.asia-east1.run.app"
RESOURCE_URL = f"{SERVICE_ORIGIN}/mcp"
LEGACY_RESOURCE_URL = f"{RESOURCE_URL}/"
CLAUDE_REDIRECT = "https://claude.ai/api/mcp/auth_callback"
HOST = "ga4-analytics-service-398991472921.asia-east1.run.app"


def _private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


os.environ.update(
    {
        "AUTH_MODE": "oauth",
        "OAUTH_ISSUER_URL": SERVICE_ORIGIN,
        "MCP_PUBLIC_URL": RESOURCE_URL,
        "OAUTH_ALLOWED_EMAILS": "owner@example.com",
        "MCP_OAUTH_CLIENT_ID": "claude-web-test",
        "GOOGLE_OAUTH_CLIENT_ID": "google-test-client",
        "GOOGLE_OAUTH_CLIENT_SECRET": "google-test-secret",
        "MCP_TOKEN_SIGNING_PRIVATE_KEY": _private_key_pem(),
    }
)

from starlette.testclient import TestClient  # noqa: E402

from capability_registry import CAPABILITY_REGISTRY_VERSION, SERVER_INSTRUCTIONS, capability_registry  # noqa: E402
from mcp_server import app  # noqa: E402
from oauth_auth import oauth_runtime  # noqa: E402


class FakeGoogleIdentity:
    def __init__(self, email: str = "owner@example.com"):
        self.email = email
        self.expected_nonce: str | None = None

    async def exchange_code(self, code: str, expected_nonce: str):
        if code != "google-code":
            raise ValueError("unexpected Google code")
        self.expected_nonce = expected_nonce
        return {
            "sub": "google-user-123",
            "email": self.email,
            "email_verified": True,
            "nonce": expected_nonce,
        }


class OAuthFlowTests(unittest.TestCase):
    def test_consent_discloses_enabled_collection_and_summary_switch(self):
        import usage_logging as usage
        for summaries in (False, True):
            writer=usage.BoundedEmitter(lambda row: None,enabled=True,summaries=summaries,start_worker=False)
            with patch.object(usage,'emitter',writer):
                response,_=self._get_consent_page()
            self.assertIn('結構化事件保存180天',response.text)
            self.assertIn('量測開始起一年',response.text)
            self.assertIn('另保留2天及7天',response.text)
            self.assertIn('清理時間無固定保證',response.text)
            self.assertIn('另存30天' if summaries else '目前不保存文字摘要',response.text)
            self.assertNotIn('USAGE_IDENTITY_KEY',response.text)

    def test_usage_events_cover_http_mcp_rest_errors_and_old_clients(self):
        from datetime import date
        import usage_logging as usage
        from usage_identity import IdentitySettings, record_resolved_tenant
        from queue import Empty
        writer = usage.BoundedEmitter(lambda row: None, enabled=True, summaries=True, start_worker=False)
        def take():
            rows=[]
            while True:
                try: rows.append(writer.queue.get_nowait())
                except Empty: return rows
        tokens = self._exchange_code(self._authorize_and_consent()).json()
        headers = {**self.headers, "authorization": f"Bearer {tokens['access_token']}",
                   "accept": "application/json, text/event-stream"}
        def rpc(name, arguments, request_id=2):
            return self.client.post('/mcp/',headers=headers,json={'jsonrpc':'2.0','id':request_id,
                'method':'tools/call','params':{'name':name,'arguments':arguments}})
        with patch.object(usage,'emitter',writer), patch('usage_identity.settings',IdentitySettings(b'a'*32)):
            denied=self.client.post('/mcp',headers={**self.headers,'accept':'application/json, text/event-stream'},json={'private':'raw body'})
            self.assertEqual(denied.status_code,401)
            rows=take(); self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['request_kind'],'unclassified');self.assertEqual(rows[0]['status'],'denied')
            self.assertIsNone(rows[0]['tool_name']);self.assertIsNone(rows[0]['user_id'])
            initialized=self.client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'initialize',
                'params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'test','version':'1'}}})
            headers['mcp-session-id']=initialized.headers['mcp-session-id']
            self.client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','method':'notifications/initialized'})
            self.client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':10,'method':'tools/list','params':{}})
            self.assertEqual(take(),[])
            def traffic(**kwargs):
                usage.observe_period(date(2026,9,1),date(2026,9,7))
                record_resolved_tenant('005')
                return {'status':'ok','daily_series':[],'query_provenance':{'sql':'SECRET SQL'}}
            args={'customer_name':'private name','start_date':'2026-09-01','end_date':'2026-09-07'}
            with patch('mcp_server.get_traffic_summary',traffic):
                response=rpc('traffic_summary',args)
                self.assertEqual(response.status_code,200,response.text)
                rows=take();self.assertEqual(len(rows),2)
                self.assertEqual(rows[0]['status'],'success');self.assertEqual(rows[0]['tenant_id'],'005')
                self.assertEqual(rows[0]['requested_days'],7);self.assertEqual(rows[0]['result_row_count'],0)
                self.assertIsNone(rows[0]['request_summary']);self.assertEqual(rows[1]['request_summary_source'],'server_generated')
                self.assertNotIn('SECRET',str(rows));self.assertNotIn('private name',str(rows))
                # Wrong hint types are ignored, not analytics schema errors.
                response=rpc('traffic_summary',args|{'analysis_goal_hint':{'secret':'bad'},'request_summary':'Authorization: Bearer SECRET'})
                self.assertEqual(response.status_code,200)
                rows=take();self.assertEqual(len(rows),1);self.assertEqual(rows[0]['status'],'success')
            response=rpc('traffic_summary',{'customer_name':'missing dates'})
            self.assertEqual(response.status_code,200)
            rows=take();self.assertEqual(len(rows),1);self.assertEqual(rows[0]['error_code'],'invalid_schema')
            self.assertEqual(rows[0]['metrics'],[])
            response=rpc('get_ga4_capabilities',{'request':'比較 GA4 與 Meta 廣告花費','analysis_goal_hint':'comparison'})
            rows=take();self.assertEqual(len(rows),1);self.assertEqual(rows[0]['status'],'unsupported')
            self.assertEqual(rows[0]['request_kind'],'capability_preflight');self.assertEqual(rows[0]['analysis_subject'],'cross_source')
            response=rpc('unknown_tool',{})
            rows=take();self.assertEqual(len(rows),1);self.assertEqual(rows[0]['error_code'],'unknown_tool')
            with patch('mcp_server.get_traffic_summary',side_effect=RuntimeError('backend failed')):
                response=rpc('traffic_summary',args)
            rows=take();self.assertEqual(rows[0]['status'],'failure');self.assertEqual(rows[0]['error_code'],'backend_error')
            with patch('main.get_traffic_summary',traffic):
                response=self.client.get('/traffic-summary',headers=headers,params=args)
                self.assertEqual(response.status_code,200)
            rows=take();self.assertEqual(len(rows),2);self.assertEqual(rows[0]['transport'],'rest')
            response=self.client.get('/traffic-summary',headers=headers)
            self.assertEqual(response.status_code,422)
            rows=take();self.assertEqual(len(rows),1);self.assertEqual(rows[0]['error_code'],'invalid_schema')

    def test_usage_identity_reaches_real_mcp_rest_and_refreshed_token(self):
        from usage_identity import IdentitySettings, current_context
        config = IdentitySettings(b"a" * 32, {"claude-web-test": "claude"})
        code = self._authorize_and_consent()
        tokens = self._exchange_code(code).json()
        headers = {**self.headers, "authorization": f"Bearer {tokens['access_token']}",
                   "accept": "application/json, text/event-stream"}
        initialized = self.client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "spoofed-host", "version": "1"}},
        })
        headers["mcp-session-id"] = initialized.headers["mcp-session-id"]
        self.client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        seen = []
        def capture(*args, **kwargs):
            seen.append(current_context.get())
            return {"status": "ok"}
        with patch("usage_identity.settings", config), patch("mcp_server.get_ga4_capability_resolution", capture), patch("main.get_traffic_summary", capture):
            call = self.client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "get_ga4_capabilities", "arguments": {}}})
            self.assertEqual(call.status_code, 200, call.text)
            rest = self.client.get("/traffic-summary", headers=headers, params={
                "customer_name": "test", "start_date": "2026-09-01", "end_date": "2026-09-07"})
            self.assertEqual(rest.status_code, 200, rest.text)
            refreshed = self.client.post("/token", headers=self.headers, data={
                "grant_type": "refresh_token", "client_id": "claude-web-test", "refresh_token": tokens["refresh_token"]})
            self.assertEqual(refreshed.status_code, 200, refreshed.text)
            headers["authorization"] = f"Bearer {refreshed.json()['access_token']}"
            call = self.client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "get_ga4_capabilities", "arguments": {}}})
            self.assertEqual(call.status_code, 200, call.text)
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(ctx and ctx.authorized and ctx.user_id for ctx in seen))
        self.assertEqual(len({ctx.user_id for ctx in seen}), 1)
        self.assertEqual(len({ctx.interaction_id for ctx in seen}), 3)
        self.assertEqual([ctx.transport for ctx in seen], ["mcp", "rest", "mcp"])
        self.assertTrue(all(ctx.host == "claude" for ctx in seen))
        self.assertIsNone(current_context.get())

    @classmethod
    def setUpClass(cls):
        assert oauth_runtime.provider is not None
        cls.provider = oauth_runtime.provider
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        self.provider.google_identity = FakeGoogleIdentity()
        self.code_verifier = "v" * 64
        digest = hashlib.sha256(self.code_verifier.encode()).digest()
        self.code_challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
        self.headers = {"host": HOST}

    def _begin_authorization(self, **overrides):
        params = {
            "client_id": "claude-web-test",
            "redirect_uri": CLAUDE_REDIRECT,
            "response_type": "code",
            "code_challenge": self.code_challenge,
            "code_challenge_method": "S256",
            "state": "claude-state",
            "scope": "ga4:read",
            "resource": RESOURCE_URL,
        }
        params.update(overrides)
        params = {key: value for key, value in params.items() if value is not None}
        return self.client.get("/authorize", params=params, headers=self.headers, follow_redirects=False)

    def _authorize_and_consent(self) -> str:
        _, consent_token = self._get_consent_page()

        consent = self.client.post(
            "/oauth/consent",
            data={"consent_token": consent_token, "decision": "approve"},
            headers=self.headers,
            follow_redirects=False,
        )
        self.assertEqual(consent.status_code, 302, consent.text)
        redirect = urlparse(consent.headers["location"])
        self.assertEqual(f"{redirect.scheme}://{redirect.netloc}{redirect.path}", CLAUDE_REDIRECT)
        query = parse_qs(redirect.query)
        self.assertEqual(query["state"], ["claude-state"])
        return query["code"][0]

    def _get_consent_page(self):
        authorize = self._begin_authorization()
        self.assertEqual(authorize.status_code, 302, authorize.text)
        google_query = parse_qs(urlparse(authorize.headers["location"]).query)
        self.assertEqual(google_query["nonce"][0], self.provider._pending_logins[google_query["state"][0]].google_nonce)

        callback = self.client.get(
            "/oauth/google/callback",
            params={"state": google_query["state"][0], "code": "google-code"},
            headers=self.headers,
        )
        self.assertEqual(callback.status_code, 200, callback.text)
        self.assertIn(
            "form-action 'self' https://claude.ai",
            callback.headers["content-security-policy"],
        )
        token_match = re.search(r'name="consent_token" value="([^"]+)"', callback.text)
        self.assertIsNotNone(token_match)
        return callback, token_match.group(1)

    def _exchange_code(self, code: str, **overrides):
        data = {
            "grant_type": "authorization_code",
            "client_id": "claude-web-test",
            "code": code,
            "redirect_uri": CLAUDE_REDIRECT,
            "code_verifier": self.code_verifier,
            "resource": RESOURCE_URL,
        }
        data.update(overrides)
        return self.client.post("/token", data=data, headers=self.headers)

    def test_discovery_and_unauthenticated_endpoints(self):
        health = self.client.get("/health", headers=self.headers)
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})

        metadata = self.client.get("/.well-known/oauth-authorization-server", headers=self.headers)
        self.assertEqual(metadata.status_code, 200)
        body = metadata.json()
        self.assertEqual(body["issuer"], SERVICE_ORIGIN)
        self.assertEqual(body["authorization_endpoint"], f"{SERVICE_ORIGIN}/authorize")
        self.assertNotIn("registration_endpoint", body)
        self.assertIn("S256", body["code_challenge_methods_supported"])
        self.assertIn("none", body["token_endpoint_auth_methods_supported"])

        protected = self.client.get("/.well-known/oauth-protected-resource/mcp/", headers=self.headers)
        self.assertEqual(protected.status_code, 200)
        self.assertEqual(protected.json()["resource"], RESOURCE_URL)

        canonical_protected = self.client.get(
            "/.well-known/oauth-protected-resource/mcp", headers=self.headers
        )
        self.assertEqual(canonical_protected.status_code, 200)

        jwks = self.client.get("/.well-known/jwks.json", headers=self.headers)
        self.assertEqual(jwks.status_code, 200)
        self.assertEqual(jwks.json()["keys"][0]["alg"], "RS256")

        mcp = self.client.post("/mcp/", headers=self.headers, json={})
        self.assertEqual(mcp.status_code, 401)
        self.assertIn("resource_metadata=", mcp.headers["www-authenticate"])

        canonical_mcp = self.client.post("/mcp", headers=self.headers, json={})
        self.assertEqual(canonical_mcp.status_code, 401)

        rest = self.client.get("/traffic-summary", headers=self.headers)
        self.assertEqual(rest.status_code, 401)

    def test_full_flow_mcp_and_refresh_rotation(self):
        code = self._authorize_and_consent()
        token_response = self._exchange_code(code)
        self.assertEqual(token_response.status_code, 200, token_response.text)
        tokens = token_response.json()

        access = self.client.post(
            "/mcp",
            headers={
                **self.headers,
                "authorization": f"Bearer {tokens['access_token']}",
                "accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        self.assertEqual(access.status_code, 200, access.text)
        server_instructions = access.json()["result"]["instructions"]
        self.assertEqual(server_instructions, SERVER_INSTRUCTIONS)
        self.assertIn("never need to know or provide tenant_id", server_instructions)
        self.assertIn("as internal metadata", server_instructions)
        self.assertIn("Never invent a metric ID or SQL", server_instructions)
        self.assertIn("small_multiples layout", server_instructions)
        self.assertIn("replace the report with a table", server_instructions)
        session_headers = {
            **self.headers,
            "authorization": f"Bearer {tokens['access_token']}",
            "accept": "application/json, text/event-stream",
            "mcp-session-id": access.headers["mcp-session-id"],
        }
        initialized = self.client.post(
            "/mcp",
            headers=session_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        self.assertEqual(initialized.status_code, 202, initialized.text)
        tools = self.client.post(
            "/mcp",
            headers=session_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        self.assertEqual(tools.status_code, 200, tools.text)
        listed_tools = tools.json()["result"]["tools"]
        self.assertEqual(
            [tool["name"] for tool in listed_tools],
            list(capability_registry.public_tool_names()),
        )
        for tool in listed_tools:
            self.assertEqual(
                tool["description"],
                capability_registry.tool_description(tool["name"]),
            )
        customer_list_schema = next(
            tool for tool in listed_tools if tool["name"] == "list_available_customers"
        )["inputSchema"]
        self.assertEqual(customer_list_schema["properties"], {})
        capability_schema = next(
            tool for tool in listed_tools if tool["name"] == "get_ga4_capabilities"
        )["inputSchema"]
        self.assertIn("request", capability_schema["properties"])
        capability_description = next(
            tool for tool in listed_tools if tool["name"] == "get_ga4_capabilities"
        )["description"]
        self.assertIn("local, versioned capability metadata", capability_description)
        search_schema = next(
            tool for tool in listed_tools if tool["name"] == "search_ga4_metrics"
        )["inputSchema"]
        self.assertIn("query", search_schema["properties"])
        query_schema = next(
            tool for tool in listed_tools if tool["name"] == "query_ga4"
        )["inputSchema"]
        self.assertIn("metric_ids", query_schema["properties"])
        self.assertNotIn("project_id", query_schema["properties"])
        self.assertNotIn("profile", query_schema["properties"])
        self.assertNotIn("sql", query_schema["properties"])
        query_description = next(
            tool for tool in listed_tools if tool["name"] == "query_ga4"
        )["description"]
        self.assertIn("request-total", query_description)
        traffic_schema = next(tool for tool in listed_tools if tool["name"] == "traffic_summary")["inputSchema"]
        self.assertIn("customer_name", traffic_schema["properties"])
        self.assertNotIn("tenant_id", traffic_schema["properties"])
        traffic_description = next(
            tool for tool in listed_tools if tool["name"] == "traffic_summary"
        )["description"]
        self.assertIn("never ask the user for project_id", traffic_description)
        self.assertIn("BigQuery cost policy applies", traffic_description)
        self.assertIn("line_chart with small_multiples layout", traffic_description)
        self.assertIn(
            "exactly the current and previous series",
            " ".join(traffic_description.split()),
        )
        self.assertIn("Use daily_series directly", traffic_description)

        authorized_rest = self.client.get(
            "/traffic-summary",
            headers={**self.headers, "authorization": f"Bearer {tokens['access_token']}"},
        )
        self.assertEqual(authorized_rest.status_code, 422)

        reused_code = self._exchange_code(code)
        self.assertEqual(reused_code.status_code, 400)
        self.assertEqual(reused_code.json()["error"], "invalid_grant")

        refresh = self.client.post(
            "/token",
            headers=self.headers,
            data={
                "grant_type": "refresh_token",
                "client_id": "claude-web-test",
                "refresh_token": tokens["refresh_token"],
                "scope": "ga4:read",
                "resource": RESOURCE_URL,
            },
        )
        self.assertEqual(refresh.status_code, 200, refresh.text)
        self.assertNotEqual(refresh.json()["refresh_token"], tokens["refresh_token"])

        reused_refresh = self.client.post(
            "/token",
            headers=self.headers,
            data={
                "grant_type": "refresh_token",
                "client_id": "claude-web-test",
                "refresh_token": tokens["refresh_token"],
            },
        )
        self.assertEqual(reused_refresh.status_code, 400)
        self.assertEqual(reused_refresh.json()["error"], "invalid_grant")

    def test_consent_page_renders_registry_contract_and_security_headers(self):
        consent_page, _ = self._get_consent_page()
        self.assertEqual(consent_page.status_code, 200)
        self.assertEqual(consent_page.headers["cache-control"], "no-store")
        self.assertEqual(consent_page.headers["referrer-policy"], "no-referrer")
        self.assertEqual(consent_page.headers["x-frame-options"], "DENY")
        policy = consent_page.headers["content-security-policy"]
        self.assertIn("default-src 'none'", policy)
        self.assertIn("style-src 'unsafe-inline'", policy)
        self.assertIn("form-action 'self' https://claude.ai", policy)
        self.assertIn("frame-ancestors 'none'", policy)

        metadata = capability_registry.consent_metadata()
        self.assertIn(f"Capability registry v{CAPABILITY_REGISTRY_VERSION}", consent_page.text)
        self.assertIn("ga4:read", consent_page.text)
        self.assertIn("僅限讀取", consent_page.text)
        for capability in metadata["supported"]:
            self.assertIn(capability["label"], consent_page.text)
            self.assertIn(capability["description"], consent_page.text)
            for tool in capability["tools"]:
                self.assertIn(tool, consent_page.text)
        for item in metadata["unsupported"]:
            self.assertIn(item["label"], consent_page.text)
            self.assertIn(item["message"], consent_page.text)
        for limitation in metadata["limitations"]:
            self.assertIn(limitation, consent_page.text)

        self.assertIn("query provenance", consent_page.text)
        self.assertIn("<main class=\"page-shell\">", consent_page.text)
        self.assertIn('aria-labelledby="page-title"', consent_page.text)
        self.assertIn('<meta name="viewport"', consent_page.text)
        self.assertIn("@media (max-width: 520px)", consent_page.text)
        self.assertIn("button:focus-visible", consent_page.text)

        readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
        self.assertIn("capability_registry.py", readme)
        for tool in metadata["public_tools"]:
            self.assertIn(tool, SERVER_INSTRUCTIONS)
            self.assertIn(tool, readme)
        for capability in metadata["supported"]:
            self.assertIn(capability["label"], SERVER_INSTRUCTIONS)
            self.assertIn(capability["description"], SERVER_INSTRUCTIONS)

    def test_consent_deny_preserves_oauth_redirect_contract(self):
        _, consent_token = self._get_consent_page()
        denied = self.client.post(
            "/oauth/consent",
            data={"consent_token": consent_token, "decision": "deny"},
            headers=self.headers,
            follow_redirects=False,
        )
        self.assertEqual(denied.status_code, 302, denied.text)
        query = parse_qs(urlparse(denied.headers["location"]).query)
        self.assertEqual(query["error"], ["access_denied"])
        self.assertEqual(query["state"], ["claude-state"])
        self.assertEqual(denied.headers["cache-control"], "no-store")

    def test_wrong_resource_redirect_and_wrong_callback_rejected(self):
        missing_resource = self._begin_authorization(resource=None)
        self.assertEqual(missing_resource.status_code, 302)
        self.assertEqual(urlparse(missing_resource.headers["location"]).netloc, "accounts.google.com")

        trailing_slash_resource = self._begin_authorization(resource=LEGACY_RESOURCE_URL)
        self.assertEqual(trailing_slash_resource.status_code, 302)
        self.assertEqual(urlparse(trailing_slash_resource.headers["location"]).netloc, "accounts.google.com")

        bad_resource = self._begin_authorization(resource="https://attacker.example/mcp/")
        self.assertEqual(bad_resource.status_code, 302)
        self.assertEqual(parse_qs(urlparse(bad_resource.headers["location"]).query)["error"], ["invalid_target"])

        bad_redirect = self._begin_authorization(redirect_uri="https://attacker.example/callback")
        self.assertEqual(bad_redirect.status_code, 400)

    def test_unapproved_google_email_and_state_replay_rejected(self):
        self.provider.google_identity = FakeGoogleIdentity("intruder@example.com")
        authorize = self._begin_authorization()
        google_state = parse_qs(urlparse(authorize.headers["location"]).query)["state"][0]

        denied = self.client.get(
            "/oauth/google/callback",
            params={"state": google_state, "code": "google-code"},
            headers=self.headers,
            follow_redirects=False,
        )
        self.assertEqual(denied.status_code, 302)
        self.assertEqual(parse_qs(urlparse(denied.headers["location"]).query)["error"], ["access_denied"])

        replay = self.client.get(
            "/oauth/google/callback",
            params={"state": google_state, "code": "google-code"},
            headers=self.headers,
        )
        self.assertEqual(replay.status_code, 400)

    def test_wrong_client_and_pkce_rejected(self):
        code = self._authorize_and_consent()

        wrong_client = self._exchange_code(code, client_id="wrong-client")
        self.assertEqual(wrong_client.status_code, 401)
        self.assertEqual(wrong_client.json()["error"], "invalid_client")

        wrong_pkce = self._exchange_code(code, code_verifier="x" * 64)
        self.assertEqual(wrong_pkce.status_code, 400)
        self.assertEqual(wrong_pkce.json()["error"], "invalid_grant")


if __name__ == "__main__":
    unittest.main()
