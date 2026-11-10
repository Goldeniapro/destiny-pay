# Security model

Destiny Pay lets an AI agent prepare money operations on PayPal. The design rule is simple: **the model proposes, code decides what exactly would happen, the owner authorizes that exact operation, and only the server talks to PayPal.** This file lists the threats we designed against and the test that proves each defense. Every test runs offline with `pytest -q` (no keys needed) and in CI.

The live demo runs on the **PayPal Sandbox only**. Live mode is refused unless `ALLOW_LIVE=1` is set.

## The authorization gate

| # | Threat | Defense | Proof (tests) |
|---|---|---|---|
| 1 | The model (or a prompt injection) moves money on its own | The model has no tool that sends, cancels or pays, records a payment, or cancels a payout. It can only file a request; execution lives in `Business.resolve_confirmation`, triggered by the owner | `test_every_tool_runs`, `test_recording_a_cash_payment_needs_an_authorization`, `test_paypal_is_never_a_manual_payment_method` |
| 2 | The operation shown is not the one executed | Each proposal is rebuilt from the server's own records (recipient, email, amount in cents, items, status, amount due) and bound to a SHA-256 fingerprint. The browser must send the fingerprint it displayed (400 without it); the server re-reads the operation after claiming it and voids it if anything changed | `test_approval_needs_the_exact_fingerprint`, `test_approval_with_another_operations_fingerprint_is_refused`, `test_changing_the_bill_after_the_proposal_voids_the_approval`, `test_changing_the_invoice_after_the_proposal_voids_the_approval`, `test_record_payment_proposal_rejects_paypal_overpayment_and_tampering` |
| 3 | The invoice was edited at PayPal after the owner reviewed it | Before sending or cancelling, the invoice PayPal holds (recipient, amount, currency, items) is compared with the reviewed one | `test_invoice_edited_at_paypal_after_review_is_not_sent` |
| 4 | Double click, replay, or button and voice at the same time | The authorization is claimed atomically (`pending → executing`); only one claim can win | `test_new_proposal_supersedes_a_stale_one_and_the_right_one_runs_once`, `test_payment_announced_once_even_with_concurrent_refreshes` |
| 5 | A spoken "yes" approves something the owner didn't hear | A "yes" only counts for the single operation the code itself read back (email included) in the previous turn; replies are classified by a closed vocabulary; two operations in one turn need the buttons | `test_code_always_reads_back_what_a_yes_will_approve`, `test_yes_to_another_question_does_not_execute`, `test_two_payments_in_one_turn_cannot_be_approved_by_voice`, `test_yes_no_classifier`, `test_security_audit_classifier_bypasses_are_closed`, `test_natural_commands_confirm_only_their_own_action` |
| 6 | An injected change of a supplier's email gets paid on a careless "yes" | A payout to an email the business has never paid successfully can only be authorized with the on-screen button; the dialog flags "first payment to this email" | `test_first_payment_to_an_email_is_never_approved_by_voice`, `test_first_payment_readback_does_not_invite_a_spoken_yes` |
| 7 | Phishing through payment reminders | Reminders use PayPal's own text; the assistant can't add free text | `test_reminders_never_carry_free_text` |
| 8 | One business sees or approves another's operations | Every read and action checks ownership; the business id travels in a header, never in URLs | `test_invoices_are_isolated_between_businesses`, `test_conversation_never_crosses_businesses`, `test_proposals_from_the_tables_go_through_the_dialog` |

Every decision is kept in the **authorization log** (Security view): operation, decision, button or voice, the receipt (fingerprint prefix) and the id PayPal returned (`test_authorization_log_and_webhook_diagnostics`).

## Paying exactly once

| Threat | Defense | Proof |
|---|---|---|
| A retry after a timeout pays a supplier twice | One `sender_batch_id` per bill attempt, also used as the `PayPal-Request-Id`; an uncertain answer (timeout, 429, 5xx) blocks the bill until PayPal's own status settles it, and a resend can never fail a payout | `test_payout_timeout_blocks_paying_twice`, `test_unknown_payout_never_reopened_by_a_failed_resend`, `test_rate_limited_payout_is_unknown_not_failed`, `test_stale_starting_payout_becomes_unknown`, `test_webhook_is_verified_with_the_raw_body_and_payouts_reuse_their_request_id` |
| An approval is executed twice at PayPal | Each authorization sends its own `PayPal-Request-Id` (`dp-<confirmation id>`), reused across retries | `test_each_authorization_uses_its_own_paypal_request_id`, `test_paypal_retries_reuse_the_same_request_id` |
| Parallel payouts exceed the daily cap | Cap check, bill claim and payout insert run as one locked step; caps per payment, per business, per visitor and global | `test_parallel_payouts_respect_the_daily_cap`, `test_payout_limits`, `test_per_visitor_payout_and_token_allowances` |
| A payout to a wrong email is lost | Unclaimed payouts are shown as "sent, not claimed yet", followed up after 3 days, and can be cancelled (with authorization) so PayPal returns the money | `test_unclaimed_payout_can_be_cancelled_with_authorization`, `test_claimed_payout_cannot_be_cancelled`, `test_unclaimed_payout_gets_one_follow_up`, `test_unclaimed_payout_returned_later_is_undone` |
| The books disagree with PayPal | Bills become paid only when PayPal reports it; held payouts stay in progress; refunds take the income back once | `test_pending_payout_settles_or_rolls_back_from_paypal_status`, `test_held_payout_stays_in_progress`, `test_refunded_invoice_takes_the_income_back_once`, `test_online_income_uses_ledger_not_stale_due` |

## Webhooks

- **Signature:** verified with PayPal's verify-webhook-signature API, sending the event body exactly as delivered.
- **Cheap checks first:** the certificate host must be PayPal's and the transmission time within 15 minutes, checked before calling PayPal.
- **Retries:** each event is handled once, and is marked as seen only after it was processed, so PayPal can retry a failed one.
- **Rate limit:** per sender.
- **Proofs:** `test_webhook_requires_configuration_and_signature`, `test_webhook_marks_invoice_paid`, `test_webhook_failure_lets_paypal_retry`.

## Public demo hardening

- **Browser:** strict Content-Security-Policy (`script-src 'self'`), HSTS, `nosniff` and `frame-ancestors`/DENY. Data is always rendered with `textContent`, never as HTML.
- **Requests:** bodies are capped before parsing (413). No public API docs. Validation errors don't echo input.
- **Limits:** per-visitor rate limits and daily quotas, and a global AI token budget. IPv6 visitors are counted per /64 (`test_daily_quota_per_ip`, `test_ipv6_visitors_counted_per_64`, `test_validation_and_limits`).
- **Dependencies:** checked with `pip-audit` in CI.

## Accepted risks

- `style-src 'unsafe-inline'`: AG Grid injects its theme styles at runtime. Scripts are never inline.
- There are no user accounts. Each visitor gets a sample business behind a random 128-bit id.
- Money is stored as SQLite `REAL`. Every authorization compares amounts in integer cents.

## Reporting

This is a hackathon demo. Please report issues through the repository's issue tracker. Never include credentials.
