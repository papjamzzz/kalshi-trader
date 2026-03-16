"""
KK Trader — Notification Engine
Sends funny SMS (Twilio) + HTML email (Gmail SMTP) on every trade event.
"""

import os
import smtplib
import random
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime

# ── Twilio (SMS) ──────────────────────────────────────────────────────────────
try:
    from twilio.rest import Client as TwilioClient
    TWILIO_AVAILABLE = True
except ImportError:
    TWILIO_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────
GMAIL_FROM    = "jeremiahstepehensmith@gmail.com"
EMAIL_TO      = "underwaterfile@proton.me"
PHONE_TO      = "+14138345062"

FUNNY = {
    "buy": [
        "💸 BOT BOUGHT {ticker} @ {price}¢ ({count} contracts, ${cost}). You were probably sleeping. Respect.",
        "🎰 ENTRY FIRED — {ticker}, {side} side, {count}x @ {price}¢. Factory's running. Lights are off.",
        "🤖 The machine just spent ${cost} on {ticker} while you're out here living your life. You're welcome.",
        "📡 ORDER PLACED: {ticker} {side} @ {price}¢. {count} contracts. The grind never sleeps (you do).",
        "⚡ BOT ACTIVATED on {ticker}. Dropped ${cost} on {side}. We're either geniuses or this is a funny story later.",
    ],
    "profit": [
        "💰 PROFIT LOCKED +${gain} on {ticker}. Exit: {price}¢. Your bot is out here getting the bag. Bow down.",
        "🏆 WINNER: {ticker} +${gain} (+{pct}%). The factory without lights just paid the electric bill.",
        "💵 CASHED OUT {ticker}. +${gain}. While you were living, the machine was printing. Balance: ${balance}",
        "🎉 +${gain} baby! {ticker} delivered. Bot's record today: {wins}W / {losses}L. Still cookin'.",
        "🔥 EXIT — {ticker} up {pct}%. Took ${gain} profit. Not bad for a robot with no feelings.",
    ],
    "loss": [
        "🛑 STOP HIT on {ticker}. -${loss} ({pct}%). The bot took the L with zero emotion. Back on the hunt.",
        "📉 Cut {ticker}. -${loss}. Even legends have bad beats. Daily P&L: ${daily}. Bot's already forgotten it.",
        "🤷 {ticker} said no. Stop loss at {price}¢. -${loss}. That's trading. Bot is already scanning for revenge.",
        "❌ {ticker} stopped out. -${loss}. Risk managed. Dry powder preserved. On to the next one.",
        "🔴 STOP — {ticker} -{pct}%. That's ${loss}. Still, you didn't have to wake up for it. Silver lining.",
    ],
    "daily_limit": [
        "🚨 DAILY LOSS CAP HIT — ${limit} reached. Bot is on ice. Go touch grass. We resume when you say so.",
        "🛑 KILL SWITCH: Daily drawdown limit (${limit}) hit. Bot is benched. Your money is safe(ish).",
        "😴 BOT SLEEPING. Lost ${limit} today, which was the limit you set. Smart past-you, that one.",
    ],
    "startup": [
        "🟢 KK TRADER IS LIVE. Factory without lights is OPEN. Scanning markets. Do not disturb.",
        "⚡ BOT STARTED. Engine running. Watching {market_count} markets. You can go to sleep now.",
        "🤖 ACTIVATED. KK Trader is online and hunting edges. May the odds (somehow) be in our favor.",
    ],
    "shutdown": [
        "🔴 BOT STOPPED. Closed all positions: {positions}. Daily P&L: ${daily}. Resume when ready.",
        "😴 KK TRADER going offline. ${daily} today. {trades} trades made. Good run.",
    ],
}


def _sms(body):
    """Send SMS via Twilio. Silently skips if credentials not configured."""
    sid   = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    from_ = os.getenv("TWILIO_FROM_NUMBER", "")

    if not (sid and token and from_) or not TWILIO_AVAILABLE:
        print(f"  📵 SMS skipped (Twilio not configured): {body[:60]}...")
        return False

    try:
        client = TwilioClient(sid, token)
        client.messages.create(body=body, from_=from_, to=PHONE_TO)
        print(f"  📱 SMS sent → {PHONE_TO}")
        return True
    except Exception as e:
        print(f"  ⚠ SMS error: {e}")
        return False


def _email(subject, html_body):
    """Send HTML email via Gmail SMTP app password."""
    password = os.getenv("GMAIL_APP_PASSWORD", "")
    if not password:
        print(f"  📭 Email skipped (GMAIL_APP_PASSWORD not set): {subject}")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = GMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_FROM, password)
            server.sendmail(GMAIL_FROM, EMAIL_TO, msg.as_string())
        print(f"  📧 Email sent → {EMAIL_TO}: {subject}")
        return True
    except Exception as e:
        print(f"  ⚠ Email error: {e}")
        return False


def _pick(template_key, **kwargs):
    templates = FUNNY.get(template_key, ["Event occurred."])
    return random.choice(templates).format(**kwargs)


def _email_html(title, color, rows, footer=""):
    """Build a dark-themed HTML email matching KK's aesthetic."""
    rows_html = "".join(
        f"<tr><td style='padding:6px 12px;color:#8892a4;font-size:13px'>{k}</td>"
        f"<td style='padding:6px 12px;color:#e8eaf0;font-size:13px;font-weight:600'>{v}</td></tr>"
        for k, v in rows.items()
    )
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return f"""
<!DOCTYPE html><html><body style='margin:0;padding:0;background:#08090e;font-family:monospace'>
<div style='max-width:520px;margin:32px auto;background:#0f1117;border:1px solid #1e2130;border-radius:10px;overflow:hidden'>
  <div style='background:{color};padding:18px 24px'>
    <div style='font-size:11px;color:rgba(0,0,0,0.6);letter-spacing:2px;text-transform:uppercase'>KK TRADER</div>
    <div style='font-size:22px;font-weight:800;color:#000;margin-top:4px'>{title}</div>
  </div>
  <table style='width:100%;border-collapse:collapse;margin:8px 0'>{rows_html}</table>
  <div style='padding:16px 24px;border-top:1px solid #1e2130'>
    <div style='font-size:11px;color:#3d4560'>FACTORY WITHOUT LIGHTS · {now}</div>
    {f'<div style="font-size:12px;color:#6b7280;margin-top:8px;font-style:italic">{footer}</div>' if footer else ''}
    <a href="https://kalshi.com" style='display:inline-block;margin-top:12px;padding:8px 16px;background:#00d595;color:#000;font-weight:700;font-size:12px;border-radius:5px;text-decoration:none'>TAKE ME TO KALSHI →</a>
  </div>
</div>
</body></html>
"""


# ── Public notification functions ─────────────────────────────────────────────

def notify_buy(ticker, side, count, price_cents, cost_dollars):
    txt = _pick("buy",
        ticker=ticker, side=side.upper(), count=count,
        price=price_cents, cost=f"{cost_dollars:.2f}"
    )
    html = _email_html(
        title=f"BOUGHT {ticker}",
        color="#3861fb",
        rows={
            "Market": ticker,
            "Side": side.upper(),
            "Contracts": count,
            "Entry Price": f"{price_cents}¢",
            "Spent": f"${cost_dollars:.2f}",
        },
        footer=txt
    )
    _sms(txt)
    _email(f"🤖 KK Bought {ticker} @ {price_cents}¢", html)


def notify_profit(ticker, side, count, entry_cents, exit_cents, gain, pct, daily_pnl, wins, losses):
    txt = _pick("profit",
        ticker=ticker, gain=f"{gain:.2f}", pct=f"{pct:.1f}",
        price=exit_cents, balance=f"{daily_pnl:.2f}",
        wins=wins, losses=losses
    )
    html = _email_html(
        title=f"PROFIT +${gain:.2f}",
        color="#00d595",
        rows={
            "Market": ticker,
            "Side": side.upper(),
            "Contracts": count,
            "Entry → Exit": f"{entry_cents}¢ → {exit_cents}¢",
            "Gain": f"+${gain:.2f} (+{pct:.1f}%)",
            "Today's P&L": f"${daily_pnl:+.2f}",
            "Record Today": f"{wins}W / {losses}L",
        },
        footer=txt
    )
    _sms(txt)
    _email(f"💰 KK Profit +${gain:.2f} on {ticker}", html)


def notify_loss(ticker, side, count, entry_cents, exit_cents, loss, pct, daily_pnl):
    txt = _pick("loss",
        ticker=ticker, loss=f"{loss:.2f}", pct=f"{pct:.1f}",
        price=exit_cents, daily=f"{daily_pnl:.2f}"
    )
    html = _email_html(
        title=f"STOP HIT −${loss:.2f}",
        color="#f6465d",
        rows={
            "Market": ticker,
            "Side": side.upper(),
            "Contracts": count,
            "Entry → Exit": f"{entry_cents}¢ → {exit_cents}¢",
            "Loss": f"-${loss:.2f} (-{pct:.1f}%)",
            "Today's P&L": f"${daily_pnl:+.2f}",
        },
        footer=txt
    )
    _sms(txt)
    _email(f"🛑 KK Stop Hit −${loss:.2f} on {ticker}", html)


def notify_daily_limit(limit, actual_loss):
    txt = _pick("daily_limit", limit=f"{limit:.2f}")
    html = _email_html(
        title="DAILY LIMIT HIT",
        color="#f5a623",
        rows={
            "Daily Loss Cap": f"${limit:.2f}",
            "Actual Loss": f"${actual_loss:.2f}",
            "Status": "Bot paused — resume manually",
        },
        footer=txt
    )
    _sms(txt)
    _email(f"🚨 KK Daily Limit Hit — Bot Paused", html)


def notify_startup(market_count=0):
    txt = _pick("startup", market_count=market_count)
    _sms(txt)


def notify_shutdown(daily_pnl=0, trade_count=0, open_positions=0):
    txt = _pick("shutdown", daily=f"{daily_pnl:.2f}", trades=trade_count, positions=open_positions)
    _sms(txt)
