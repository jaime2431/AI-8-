"""Where summaries and alerts go. Console for development; a Teams/Slack incoming
webhook (stored as a secret per client) in production. Email and WhatsApp senders
plug in behind the same two methods."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Protocol
from urllib.parse import unquote, urlparse

import httpx


class Notifier(Protocol):
    def send_summary(self, client_id: str, recipients: tuple[str, ...], text: str) -> None: ...
    def alert(self, client_id: str, text: str) -> None: ...


class ConsoleNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_summary(self, client_id, recipients, text):
        self.sent.append(("summary", f"[{client_id}] -> {', '.join(recipients) or '(no recipients)'}\n{text}"))
        print(self.sent[-1][1])

    def alert(self, client_id, text):
        self.sent.append(("alert", f"[{client_id}] ALERT: {text}"))
        print(self.sent[-1][1])


class EmailNotifier:
    """Sends the owner's morning summary by email over SMTP with STARTTLS.
    smtp_url: smtp://user:password@host:587/?from=resumen@datia.do  (kept in the client's vault as 'smtp-url').
    Works with Microsoft 365 (smtp.office365.com) and Azure Communication Services SMTP."""

    def __init__(self, smtp_url: str, fallback: Notifier | None = None):
        u = urlparse(smtp_url)
        self.host, self.port = u.hostname, u.port or 587
        self.user, self.password = unquote(u.username or ""), unquote(u.password or "")
        self.sender = dict(p.split("=", 1) for p in u.query.split("&") if "=" in p).get("from", self.user)
        self.fallback = fallback or ConsoleNotifier()

    def send_summary(self, client_id, recipients, text):
        if not recipients:
            self.fallback.send_summary(client_id, recipients, text)
            return
        msg = EmailMessage()
        msg["Subject"] = text.splitlines()[0][:120] if text else "Resumen del día"
        msg["From"], msg["To"] = self.sender, ", ".join(recipients)
        msg.set_content(text)
        with smtplib.SMTP(self.host, self.port, timeout=30) as s:
            s.starttls()
            if self.user:
                s.login(self.user, self.password)
            s.send_message(msg)

    def alert(self, client_id, text):
        self.fallback.alert(client_id, text)


def webhook_payload(url: str, text: str) -> dict:
    """Slack incoming webhooks take {"text"}; Microsoft Teams Workflows webhooks (logic.azure.com) and the
    older Office 365 connectors (webhook.office.com) take an Adaptive Card."""
    if "slack.com" in url:
        return {"text": text}
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
        "body": [{"type": "TextBlock", "text": text, "wrap": True}]}}]}


class WebhookNotifier:
    """Operational alerts to OUR team's channel for this client (Teams or Slack). The owner's summary itself is
    sent by the summary sender (email/WhatsApp); only a one-line notice goes to the channel, never the figures."""

    def __init__(self, webhook_url: str, fallback: Notifier | None = None, summary_sender: Notifier | None = None):
        self.url, self.fallback, self.summary_sender = webhook_url, fallback or ConsoleNotifier(), summary_sender

    def _post(self, text: str):
        httpx.post(self.url, json=webhook_payload(self.url, text), timeout=20).raise_for_status()

    def send_summary(self, client_id, recipients, text):
        (self.summary_sender or self.fallback).send_summary(client_id, recipients, text)
        try:
            self._post(f"Resumen del día enviado a {client_id} ({len(recipients)} destinatarios).")
        except Exception as e:
            self.fallback.alert(client_id, f"channel notice failed: {e}")

    def alert(self, client_id, text):
        try:
            self._post(f"ALERTA {client_id}: {text}")
        except Exception as e:                   # an alert must never vanish: at least the job log has it
            self.fallback.alert(client_id, f"{text} (webhook failed: {e})")
