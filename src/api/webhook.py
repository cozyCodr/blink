# src/api/webhook.py
"""
HMAC-SHA256 signed async Webhook Dispatcher for Warden.
Handles subscription management, payload signing, and async delivery.
"""
import hmac
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any
import httpx
from pydantic import BaseModel, Field

def compute_signature(secret: str, payload_bytes: bytes) -> str:
    """Computes HMAC-SHA256 hex digest prefixed with 'sha256='."""
    mac = hmac.new(secret.encode("utf-8"), msg=payload_bytes, digestmod=hashlib.sha256)
    return f"sha256={mac.hexdigest()}"

def verify_signature(secret: str, payload_bytes: bytes, header_signature: str) -> bool:
    """Constant-time verification of HMAC-SHA256 signature."""
    expected = compute_signature(secret, payload_bytes)
    return hmac.compare_digest(expected, header_signature)

class WebhookSubscription(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    workspace_id: str
    url: str
    secret: str
    event_types: List[str] = Field(default_factory=lambda: ["*"])
    active: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class WebhookDeliveryResult(BaseModel):
    subscription_id: str
    url: str
    status_code: Optional[int] = None
    success: bool
    error: Optional[str] = None
    delivered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class WebhookDispatcher:
    """Manages workspace webhook subscriptions and asynchronous HTTP dispatch."""

    def __init__(self):
        # workspace_id -> Dict[sub_id, WebhookSubscription]
        self._subscriptions: Dict[str, Dict[str, WebhookSubscription]] = {}

    def register_subscription(
        self,
        workspace_id: str,
        url: str,
        secret: str,
        event_types: Optional[List[str]] = None
    ) -> WebhookSubscription:
        sub = WebhookSubscription(
            workspace_id=workspace_id,
            url=url,
            secret=secret,
            event_types=event_types or ["*"]
        )
        if workspace_id not in self._subscriptions:
            self._subscriptions[workspace_id] = {}
        self._subscriptions[workspace_id][sub.id] = sub
        return sub

    def list_subscriptions(self, workspace_id: str) -> List[WebhookSubscription]:
        return list(self._subscriptions.get(workspace_id, {}).values())

    def remove_subscription(self, workspace_id: str, subscription_id: str) -> bool:
        if workspace_id in self._subscriptions and subscription_id in self._subscriptions[workspace_id]:
            del self._subscriptions[workspace_id][subscription_id]
            return True
        return False

    async def dispatch_event(
        self,
        workspace_id: str,
        event_type: str,
        payload: Dict[str, Any],
        timeout: float = 5.0
    ) -> List[WebhookDeliveryResult]:
        subs = self.list_subscriptions(workspace_id)
        matching_subs = [
            s for s in subs
            if s.active and ("*" in s.event_types or event_type in s.event_types)
        ]

        if not matching_subs:
            return []

        envelope = {
            "event": event_type,
            "workspace_id": workspace_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload
        }
        body_bytes = json.dumps(envelope, separators=(",", ":")).encode("utf-8")

        results: List[WebhookDeliveryResult] = []
        async with httpx.AsyncClient(timeout=timeout) as client:
            for sub in matching_subs:
                signature = compute_signature(sub.secret, body_bytes)
                headers = {
                    "Content-Type": "application/json",
                    "X-Warden-Signature": signature,
                    "X-Warden-Event": event_type,
                    "X-Warden-Workspace": workspace_id
                }
                try:
                    resp = await client.post(sub.url, content=body_bytes, headers=headers)
                    results.append(WebhookDeliveryResult(
                        subscription_id=sub.id,
                        url=sub.url,
                        status_code=resp.status_code,
                        success=resp.is_success
                    ))
                except Exception as ex:
                    results.append(WebhookDeliveryResult(
                        subscription_id=sub.id,
                        url=sub.url,
                        status_code=None,
                        success=False,
                        error=str(ex)
                    ))

        return results

# Global dispatcher instance
webhook_dispatcher = WebhookDispatcher()
