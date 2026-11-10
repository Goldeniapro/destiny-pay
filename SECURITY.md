# Security model

Destiny Pay lets an AI agent prepare money operations on PayPal. The design rule is simple: **the model proposes, code decides what exactly would happen, the owner authorizes that exact operation, and only the server talks to PayPal.** This file lists the threats we designed against and the test that proves each defense. Every test runs offline with `pytest -q` (no keys needed) and in CI.

The live demo runs on the **PayPal Sandbox only**. Live mode is refused unless `ALLOW_LIVE=1` is set.

## The authorization gate

| # | Threat | Defense | Proof (tests) |
|---|---|---|---|
| 1 | The model (or a prompt injection) moves money or settles a debt on its own | The model has no tool that sends, cancels or pays an invoice, records a payment on an invoice, marks a bill paid, or cancels a payout. It can only file a request; execution lives in `Business.resolve_confirmation`, triggered by the owner (see accepted risks for dictated cash notes) | `test_every_tool_runs`, `test_recording_a_cash_payment_needs_an_authorization`, `test_marking_a_bill_paid_needs_an_authorization`, `test_paypal_is_never_a_manual_payment_method` |
| 2 | The operation shown is not the one executed | Each proposal is rebuilt from the server's own records (recipient, email, amount in cents, items, status, amount due) and bound to a SHA-256 fingerprint. The browser must send the fingerprint it displayed (400 without it); the server re-reads the operation after claiming it and voids it if anything changed. An approval carrying another fingerprint runs nothing and leaves the owner's card intact, so a forged request can't void it | `test_approval_needs_the_exact_fingerprint`, `test_approval_with_another_operations_fingerprint_is_refused`, `test_changing_the_bill_after_the_proposal_voids_the_approval`, `test_changing_the_invoice_after_the_proposal_voids_the_approval`, `test_record_payment_proposal_rejects_paypal_overpayment_and_tampering` |
| 3 | The invoice was edited at PayPal after the owner reviewed it | Before sending or cancelling, the invoice PayPal holds (recipient, amount, currency, items) is compared with the reviewed one | `test_invoice_edited_at_paypal_after_review_is_not_sent` |
| 4 | Double click, replay, button and voice at the same time, or two authorizations on one invoice | The authorization is claimed atomically (`pending → executing`); only one claim can win, and only one operation per invoice or bill runs at a time, so two payments never validate against the same balance | `test_new_proposal_supersedes_a_stale_one_and_the_right_one_runs_once`, `test_payment_announced_once_even_with_concurrent_refreshes`, `test_two_payments_on_one_invoice_never_validate_against_the_same_balance` |
| 5 | A spoken "yes" approves something the owner didn't hear | A "yes" only counts for the single operation the code itself read back (email included) in the previous turn; the "yes" is armed only after that readback was built; replies are classified by a closed vocabulary; two operations in one turn need the buttons. In the dialog, a spoken answer is classified on the server | `test_code_always_reads_back_what_a_yes_will_approve`, `test_spoken_yes_records_a_cash_payment_only_after_the_code_read_it_back`, `test_spoken_yes_cancels_an_unclaimed_payout_after_the_readback`, `test_a_readback_that_cannot_be_built_never_arms_a_spoken_yes`, `test_spoken_yes_inside_the_dialog_is_decided_by_the_server`, `test_first_payment_is_never_approved_by_voice_in_the_dialog`, `test_yes_to_another_question_does_not_execute`, `test_two_payments_in_one_turn_cannot_be_approved_by_voice`, `test_yes_no_classifier`, `test_security_audit_classifier_bypasses_are_closed`, `test_natural_commands_confirm_only_their_own_action` |
| 6 | An injected change of a supplier's email gets paid on a careless "yes" | A payout to an email the business has never paid successfully can only be authorized with the on-screen button; the dialog flags "first payment to this email" | `test_first_payment_to_an_email_is_never_approved_by_voice`, `test_first_payment_readback_does_not_invite_a_spoken_yes` |
| 7 | Phishing through payment reminders | Reminders use PayPal's own text; the assistant can't add free text | `test_reminders_never_carry_free_text` |
| 8 | One business sees or approves another's operations | Every read and action checks ownership; the business id travels in a header, never in URLs | `test_invoices_are_isolated_between_businesses`, `test_conversation_never_crosses_businesses`, `test_proposals_from_the_tables_go_through_the_dialog` |

Every decision is kept in the **authorization log** (Security view): operation, decision, button or voice, the receipt (fingerprint prefix) and the id PayPal returned (`test_authorization_log_and_webhook_diagnostics`).

## Paying exactly once

| Threat | Defense | Proof |
|---|---|---|
| A retry after a timeout pays a supplier twice | One `sender_batch_id` per bill attempt, also used as the `PayPal-Request-Id`; an uncertain answer (timeout, 429, 5xx) blocks the bill until PayPal's own status settles it, and a resend can never fail a payout | `test_payout_timeout_blocks_paying_twice`, `test_unknown_payout_never_reopened_by_a_failed_resend`, `test_rate_limited_payout_is_unknown_not_failed`, `test_stale_starting_payout_becomes_unknown`, `test_webhook_is_verified_with_the_raw_body_and_payouts_reuse_their_request_id` |
| An approval is executed twice at PayPal | Sending or cancelling an invoice and cancelling a payout send `PayPal-Request-Id: dp-<confirmation id>`, reused across retries. Recording a cash payment uses an id derived from the operation itself (invoice, amount, balance due), so re-authorizing the same payment after a lost answer reuses it. Payouts use the `sender_batch_id` (above) | `test_each_authorization_uses_its_own_paypal_request_id`, `test_paypal_retries_reuse_the_same_request_id`, `test_record_payment_with_a_lost_answer_is_booked_once_and_never_recorded_twice` |
| PayPal recorded a payment but its answer was lost | The books are reconciled from PayPal's own list of payments on the invoice (`payments.transactions`, by `payment_id`), each counted once | `test_record_payment_with_a_lost_answer_is_booked_once_and_never_recorded_twice`, `test_record_payment_uses_paypal_payment_id` |
| A payout stays unconfirmed forever | After 10 minutes the inbox asks the owner to look in PayPal; if it was paid, they record it through the same authorization dialog. Nothing is re-paid | `test_stuck_payout_gets_an_authorized_way_out` |
| Parallel payouts exceed the daily cap | Cap check, bill claim and payout insert run as one locked step; caps per payment, per business, per visitor and global | `test_parallel_payouts_respect_the_daily_cap`, `test_payout_limits`, `test_per_visitor_payout_and_token_allowances` |
| A payout to a wrong email is lost | Unclaimed payouts are shown as "sent, not claimed yet", followed up after 3 days, and can be cancelled (with authorization) so PayPal returns the money | `test_unclaimed_payout_can_be_cancelled_with_authorization`, `test_claimed_payout_cannot_be_cancelled`, `test_cancelled_payout_on_a_recurring_bill_does_not_duplicate_the_next_one`, `test_unclaimed_payout_gets_one_follow_up`, `test_unclaimed_payout_returned_later_is_undone` |
| The books disagree with PayPal | PayPal payouts count as paid only when PayPal reports it; held payouts stay in progress; refunds take back what PayPal reports as refunded (`refunds.refund_amount`, partial refunds included), once | `test_pending_payout_settles_or_rolls_back_from_paypal_status`, `test_held_payout_stays_in_progress`, `test_refunded_invoice_takes_the_income_back_once`, `test_partial_refund_uses_paypal_refund_amount`, `test_online_income_uses_ledger_not_stale_due` |

## Webhooks

- **Signature:** verified with PayPal's verify-webhook-signature API, sending the event body exactly as delivered.
- **Cheap checks first:** the certificate host must be PayPal's and the transmission time within 15 minutes, checked before calling PayPal.
- **Retries:** each event is handled once (kept in the database, so a restart doesn't make an old delivery new), and is marked as seen only after it was processed, so PayPal can retry a failed one.
- **No cross-business signal:** each business sees only its own last notification (Security view); refusals go to the server log, never to a public endpoint.
- **Rate limit:** per sender.
- **Proofs:** `test_webhook_requires_configuration_and_signature`, `test_webhook_marks_invoice_paid`, `test_webhook_failure_lets_paypal_retry`, `test_authorization_log_and_webhook_diagnostics`.

## Public demo hardening

- **Browser:** strict Content-Security-Policy (`script-src 'self'`), HSTS, `nosniff` and `frame-ancestors`/DENY. Data is always rendered with `textContent`, never as HTML.
- **Requests:** bodies are capped before parsing (413). No public API docs. Validation errors don't echo input.
- **Limits:** per-visitor rate limits and daily quotas, and a global AI token budget. IPv6 visitors are counted per /64 (`test_daily_quota_per_ip`, `test_ipv6_visitors_counted_per_64`, `test_validation_and_limits`).
- **Dependencies:** checked with `pip-audit` in CI.

## Accepted risks

- `style-src 'unsafe-inline'`: AG Grid injects its theme styles at runtime. Scripts are never inline.
- There are no user accounts. Each visitor gets a sample business behind a random 128-bit id, sent in a header: whoever holds that id is the owner.
- Cash notes the owner dictates without an invoice or bill ("I sold 3 coffees in cash", "I spent 8 on a taxi") are written directly with `record_movement`. They never touch PayPal, settle no invoice or bill, and appear in the activity log. Asking for a confirmation on every note would break the voice flow.
- Per-visitor limits trust the client address set by the hosting edge (`X-Real-IP` on Railway).
- Money is stored as SQLite `REAL`. Every authorization compares amounts in integer cents.

## Reporting

This is a hackathon demo. Please report issues through the repository's issue tracker. Never include credentials.
