"""
Module 6: Notifier
───────────────────
Sends SMS (Twilio) and email (SendGrid) notifications for:
  - Trade executions (immediate)
  - Daily digest (end of cycle summary)
  - Bot errors / kill switch triggers (immediate)
  - Stop-loss / take-profit events (immediate)
"""

import os
import sys
import logging
from datetime import datetime, timezone
from typing import Optional

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from config.settings import (
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
    TWILIO_FROM_NUMBER, ALERT_PHONE_NUMBER,
    SENDGRID_API_KEY, ALERT_EMAIL_FROM, ALERT_EMAIL_TO,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: SMS via Twilio
# ══════════════════════════════════════════════════════════════════════════════

def send_sms(message: str) -> bool:
    """Send an SMS alert via Twilio. Returns True on success."""
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN,
                TWILIO_FROM_NUMBER, ALERT_PHONE_NUMBER]):
        log.warning("Twilio not configured — skipping SMS")
        return False
    try:
        from twilio.rest import Client
        client  = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        msg     = client.messages.create(
            body=message[:1600],
            from_=TWILIO_FROM_NUMBER,
            to=ALERT_PHONE_NUMBER,
        )
        log.info(f"SMS sent (SID: {msg.sid})")
        return True
    except Exception as e:
        log.error(f"SMS failed: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: Email via SendGrid
# ══════════════════════════════════════════════════════════════════════════════

def send_email(subject: str, body_text: str, body_html: Optional[str] = None) -> bool:
    """Send an email alert via SendGrid. Returns True on success."""
    if not all([SENDGRID_API_KEY, ALERT_EMAIL_FROM, ALERT_EMAIL_TO]):
        log.warning("SendGrid not configured — skipping email")
        return False
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, Content

        html = body_html or f"<pre>{body_text}</pre>"
        message = Mail(
            from_email=ALERT_EMAIL_FROM,
            to_emails=ALERT_EMAIL_TO,
            subject=subject,
            html_content=html,
        )
        sg     = SendGridAPIClient(SENDGRID_API_KEY)
        resp   = sg.send(message)
        log.info(f"Email sent (status: {resp.status_code})")
        return resp.status_code in (200, 202)
    except Exception as e:
        log.error(f"Email failed: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: Notification templates
# ══════════════════════════════════════════════════════════════════════════════

def notify_trade(decision: dict):
    """Send immediate alert for a single executed trade."""
    ticker  = decision["ticker"]
    action  = decision["action"].upper()
    qty     = decision.get("quantity", 0)
    price   = decision.get("price", 0)
    total   = decision.get("total_usd", 0)
    conf    = decision.get("confidence", 0)
    status  = decision.get("status", "unknown").upper()

    emoji = {"BUY": "[BUY]", "SELL": "[SELL]"}.get(action, "[TRADE]")

    sms = (
        f"{emoji} Trading Bot\n"
        f"{action} {qty:.2f} {ticker} @ ${price:.2f}\n"
        f"Total: ${total:.2f} | Conf: {conf:.0%} | {status}"
    )

    subject = f"{emoji} {action} {ticker} — ${total:.2f}"
    html = f"""
    <div style="font-family: sans-serif; max-width: 480px;">
      <h2 style="color: {'#1a7f37' if action=='BUY' else '#cf222e'};">
        {action} {ticker}
      </h2>
      <table style="width:100%; border-collapse:collapse;">
        <tr><td><b>Shares</b></td><td>{qty:.4f}</td></tr>
        <tr><td><b>Price</b></td><td>${price:.2f}</td></tr>
        <tr><td><b>Total</b></td><td>${total:.2f}</td></tr>
        <tr><td><b>Confidence</b></td><td>{conf:.0%}</td></tr>
        <tr><td><b>Status</b></td><td>{status}</td></tr>
        <tr><td><b>Arbitration</b></td><td>{decision.get('arbitration','')}</td></tr>
      </table>
      <hr/>
      <p style="font-size:12px; color:#666;">
        {decision.get('reasoning','')[:400]}
      </p>
    </div>
    """

    send_sms(sms)
    send_email(subject, sms, html)


def notify_daily_digest(
    executed: list,
    blocked: list,
    held: list,
    account_info: dict,
):
    """Send end-of-cycle summary."""
    now     = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    equity  = account_info.get("equity", 0)
    pnl     = account_info.get("pnl_today", 0)
    pnl_sym = "+" if pnl >= 0 else ""

    lines = [
        f"Trading Bot Daily Digest — {now}",
        f"Account equity: ${equity:,.2f} ({pnl_sym}${pnl:,.2f} today)",
        "",
    ]

    if executed:
        lines.append(f"EXECUTED ({len(executed)}):")
        for d in executed:
            lines.append(
                f"  {d['action'].upper()} {d['ticker']} "
                f"{d.get('quantity',0):.2f} shares @ ${d.get('price',0):.2f}"
            )
    if blocked:
        lines.append(f"\nBLOCKED ({len(blocked)}):")
        for d in blocked:
            lines.append(f"  {d['ticker']}: {d.get('blocked_reason','')}")
    if held:
        lines.append(f"\nHELD ({len(held)}): {', '.join(held)}")

    body = "\n".join(lines)
    sms  = f"Bot digest: {len(executed)} trades, equity ${equity:,.0f} ({pnl_sym}${pnl:,.0f})"

    send_sms(sms)
    send_email(f"Trading Bot Digest — {now}", body)


def notify_error(message: str):
    """Send an error/critical alert."""
    send_sms(f"[ERROR] Trading Bot\n{message[:200]}")
    send_email(f"[ERROR] Trading Bot Alert", message)


def notify_stop_loss(ticker: str, pct: float, proceeds: float):
    """Send stop-loss triggered alert."""
    msg = (
        f"[STOP-LOSS] {ticker} sold at {pct:.1f}% loss. "
        f"Proceeds: ${proceeds:.2f}"
    )
    send_sms(msg)
    send_email(f"[STOP-LOSS] {ticker}", msg)


# ══════════════════════════════════════════════════════════════════════════════
# Quick test
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n=== Module 6: Notifier — Test Run ===\n")
    print("Sending test SMS and email...")

    sms_ok   = send_sms("Trading Bot: test notification. If you see this, SMS is working.")
    email_ok = send_email(
        subject="Trading Bot: test notification",
        body_text="If you received this, SendGrid email is configured correctly.",
    )

    print(f"  SMS:   {'sent' if sms_ok else 'failed (check TWILIO keys)'}")
    print(f"  Email: {'sent' if email_ok else 'failed (check SENDGRID keys)'}")
