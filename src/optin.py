"""Opt-in / opt-out website for flood alerts (Streamlit, functions only).

Run from the project root:
    uv run --env-file twilio.env streamlit run src/optin.py

Without --env-file it still works, but confirmation texts are skipped (dry run).
Subscriptions are stored in data/subscribers.db, which server.py reads.
"""

import streamlit as st

import db
import send as sms

OPT_IN_TEXT = "You're subscribed to Red River flood alerts. Reply STOP to opt out."
OPT_OUT_TEXT = "You've been unsubscribed from Red River flood alerts. You won't get more texts."
CONSENT_TEXT = ("I agree to receive flood alert text messages at this number. "
                "Message and data rates may apply. Reply STOP at any time to opt out.")


def send_confirmation(phone, text):
    """Send a confirmation text. Returns an error message, or None if it worked."""
    _, error = sms.send_sms(phone, text)
    if error and error.startswith(str(sms.ERROR_OPTED_OUT)):
        return ("this number previously replied STOP to our texts, so the carrier is "
                f"blocking them. Text START to {sms.FROM_NUMBER} to allow alerts again.")
    return error


def handle_opt_in(raw_phone, raw_postal, consent):
    if not db.is_valid_phone(raw_phone):
        st.error("Enter a valid 10-digit phone number, e.g. 647-555-0123.")
        return
    if not consent:
        st.error("Please tick the consent box to opt in.")
        return

    phone = db.to_e164(raw_phone)
    if not db.subscribe(phone, db.clean_postal(raw_postal)):
        st.info(f"{db.pretty_phone(phone)} is already subscribed.")
        return

    error = send_confirmation(phone, OPT_IN_TEXT)
    if error:
        st.warning(f"You're subscribed, but the confirmation text didn't send: {error}")
    else:
        st.success(f"You're subscribed! A confirmation text is on its way to "
                   f"{db.pretty_phone(phone)}.")


def handle_opt_out(raw_phone):
    if not db.is_valid_phone(raw_phone):
        st.error("Enter a valid 10-digit phone number, e.g. 647-555-0123.")
        return

    phone = db.to_e164(raw_phone)
    if not db.unsubscribe(phone):
        st.info(f"{db.pretty_phone(phone)} isn't subscribed, so there's nothing to cancel.")
        return

    send_confirmation(phone, OPT_OUT_TEXT)   # best effort; opt-out stands either way
    st.success(f"{db.pretty_phone(phone)} has been unsubscribed. You won't receive more alerts.")


def main():
    st.set_page_config(page_title="Red River Flood Alerts", page_icon="🌊", layout="centered")

    st.title("🌊 Red River Flood Alerts")
    st.write("Get free text alerts when flood risk changes near you, plus seasonal "
             "outlooks every couple of months. Opt out any time.")

    with st.form("subscription"):
        phone = st.text_input("Phone number", placeholder="647-555-0123")
        postal = st.text_input("Postal code (optional)", placeholder="R3C 4T3",
                               help="Used to send you alerts for your area.")
        consent = st.checkbox(CONSENT_TEXT)
        left, right = st.columns(2)
        opt_in = left.form_submit_button("Opt in", type="primary")
        opt_out = right.form_submit_button("Opt out")

    if opt_in:
        handle_opt_in(phone, postal, consent)
    elif opt_out:
        handle_opt_out(phone)

    note = " · test mode, no texts are sent" if sms.DRY_RUN else ""
    st.caption(f"{db.count_active()} people subscribed{note}")


main()
