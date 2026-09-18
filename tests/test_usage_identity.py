import asyncio
import base64
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from starlette.requests import Request

from query_policy import QueryPolicyError
from usage_identity import (
    AnalyticsContext, IdentitySettings, MCPIdentityMiddleware, bind_context, current_context,
    record_resolved_tenant, require_tenant_access, trusted_context,
)


def token(subject="alice", client="claude-id", scopes=None):
    return AccessToken(token="never-store", client_id=client, subject=subject,
                       scopes=["ga4:read"] if scopes is None else scopes)


class IdentityTests(unittest.TestCase):
    def test_stable_subject_across_transport_host_and_refresh(self):
        config = IdentitySettings(b"a" * 32, {"claude-id": "claude", "chatgpt-id": "chatgpt"})
        contexts = [trusted_context(token(client=client), mode="oauth", transport=transport,
                                    identity_settings=config)
                    for client, transport in (("claude-id", "mcp"), ("chatgpt-id", "mcp"), ("claude-id", "rest"))]
        self.assertEqual(len({ctx.user_id for ctx in contexts}), 1)
        self.assertEqual([ctx.host for ctx in contexts], ["claude", "chatgpt", "claude"])
        self.assertNotEqual(contexts[0].user_id, trusted_context(token("bob"), mode="oauth", transport="mcp", identity_settings=config).user_id)
        self.assertNotIn("alice", repr(contexts[0]))
        self.assertNotIn("never-store", repr(contexts[0]))
        self.assertIsNone(current_context.get())

    def test_iam_is_not_a_person_and_missing_oauth_denies(self):
        ctx = trusted_context(token(), mode="cloud-run-iam", transport="rest")
        self.assertIsNone(ctx.user_id)
        self.assertFalse(ctx.authenticated)
        for access in (None, token(subject=""), token(scopes=[])):
            ctx = trusted_context(access, mode="oauth", transport="mcp")
            with bind_context(ctx), self.assertRaises(QueryPolicyError) as raised:
                require_tenant_access()
            self.assertEqual(raised.exception.code, "tenant_access_denied")

    def test_secret_configuration_fails_closed_without_echoing_values(self):
        cases = ({"USAGE_ENABLED": "true", "USAGE_IDENTITY_KEY": ""},
                 {"USAGE_IDENTITY_KEY": "not-base64-secret"},
                 {"USAGE_IDENTITY_KEY": base64.b64encode(b"short").decode()},
                 {"USAGE_CLIENT_HOSTS": '{"client":"untrusted-host"}'})
        for values in cases:
            with self.subTest(values=list(values)), patch.dict(os.environ, values, clear=True):
                with self.assertRaises(RuntimeError):
                    IdentitySettings.from_env()

    def test_missing_or_faulty_pseudonym_does_not_revoke_authorization(self):
        for settings in (IdentitySettings(), IdentitySettings(b"a" * 32)):
            with patch("usage_identity.hmac.new", side_effect=RuntimeError("secret")):
                ctx = trusted_context(token(), mode="oauth", transport="mcp", identity_settings=settings)
            self.assertIsNone(ctx.user_id)
            with bind_context(ctx):
                require_tenant_access()
                record_resolved_tenant("005")
            self.assertEqual(ctx.tenant_id, "005")
            self.assertEqual(ctx.authorization_result, "allowed")

    def test_tenant_type_failure_only_lowers_quality(self):
        ctx = trusted_context(token(), mode="oauth", transport="mcp")
        with bind_context(ctx):
            for value in ("5", "005", "tenant-a", None, 5):
                record_resolved_tenant(value)
                self.assertEqual(ctx.tenant_id, value if isinstance(value, str) else None)
                require_tenant_access()

    def test_denial_precedes_bigquery_credentials(self):
        import main
        with bind_context(AnalyticsContext(transport="rest")), patch("main.google.auth.default") as credentials:
            with self.assertRaises(QueryPolicyError):
                main.get_bigquery_client()
        credentials.assert_not_called()

    def test_resolved_inactive_and_unavailable_tenants_remain_observable(self):
        import main
        for status, project in (("inactive", "project"), ("active", None), ("active", "project")):
            row = SimpleNamespace(tenant_id="005", tenant_name="test", status=status, project_id=project, ec=False)
            ctx = trusted_context(token(), mode="oauth", transport="mcp")
            with bind_context(ctx), patch("main._resolve_tenant_record", return_value=(row, "test", "test", "exact")), patch("main.get_bigquery_client"):
                main.get_customer_status("test")
                self.assertEqual(ctx.tenant_id, "005")
                self.assertEqual(ctx.authorization_result, "allowed" if status == "active" else "denied")
                ctx.tenant_id = None
                try:
                    main.get_tenant_config(None, "test")
                except main.TenantResolutionError:
                    pass
                self.assertEqual(ctx.tenant_id, "005")


class ConcurrentIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_middleware_uses_each_http_user_and_resets_on_exceptions(self):
        runtime = SimpleNamespace(mode="oauth", config=SimpleNamespace(required_scope="ga4:read"))
        middleware = MCPIdentityMiddleware(runtime)
        config = IdentitySettings(b"a" * 32, {"claude-id": "claude"})

        async def run(subject, tenant_id, fail=False):
            request = Request({"type": "http", "user": AuthenticatedUser(token(subject)),
                               "headers": [(b"x-user-id", b"spoofed")]})
            async def handler(ctx):
                before = current_context.get()
                await asyncio.sleep(0)
                record_resolved_tenant(tenant_id)
                self.assertIs(current_context.get(), before)
                if fail:
                    raise ValueError("handler failure")
                return before
            return await middleware(SimpleNamespace(request=request), handler)

        with patch("usage_identity.settings", config):
            a, b = await asyncio.gather(run("alice", "005"), run("bob", "5"))
            with self.assertRaises(ValueError):
                await run("alice", "tenant-a", fail=True)
        self.assertNotEqual(a.user_id, b.user_id)
        self.assertNotEqual(a.interaction_id, b.interaction_id)
        self.assertEqual((a.tenant_id, b.tenant_id), ("005", "5"))
        self.assertIsNone(current_context.get())
