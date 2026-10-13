# Usability comparison protocol (T-14)

Status: **protocol ready; no sessions run yet.** No participant data exists. Do not fill
`results_template.csv` with anything other than observed session data.

## Question

For everyday multi-step payments, does the conversational flow (DCTA chat, `#/chat`) let
people finish faster and with fewer mistakes than a conventional form (`#/baseline`), and do
people know exactly what they are approving (recipient, source account, total debit) before
they approve? With about five testers this is **exploratory**: report what was observed,
never a significance claim.

## Participants and consent

- About 5 adults who use mobile or online banking, recruited informally (friends,
  colleagues, hackathon peers). No one under 18. No payment or incentive is required.
- Everyone uses the same **synthetic** demo user (`user-matthew`) and mock accounts from
  `ledger/seed.py`. Testers never enter, show or mention their own bank details. If a tester
  starts to, stop them and remove it from the notes.
- Read the consent text in `facilitator_script.md` before starting. Record consent (yes/no)
  and, separately, permission to quote their words (yes/no) in the results file.
  Without consent, no session.
- **Recording**: no screen or audio recording by default. Voice input is optional; if a
  tester uses the microphone, the gateway stores the clip as audio evidence in the local
  access-controlled store (never in Langfuse). Ask for explicit voice consent first;
  otherwise use typed input only.
- **Retention**: results use participant codes (P1–P5), never names. Delete facilitator
  notes, any audio clips from these sessions, and the code-to-name key (if one was kept at
  all) by **15 November 2026**, or immediately if a tester withdraws. Withdrawal is possible
  at any time, including afterwards, with no reason needed.
- Only aggregated numbers and consented quotes leave the facilitator's machine.

## Setup (per session)

1. Clean start: `make reset-db` (fresh synthetic seed), start gateway/agent/validator and
   the web app as for the demo, sign in with the demo passkey.
2. Turn on the facilitator timer by opening `http://localhost:5173/?facilitator=1#/chat` (`?facilitator=0` turns it off; the baseline form is at `#/baseline`, which is not in the navigation); it counts control
   activations and time per task on both pages and stores nothing outside the page. While it
   is on, the chat hides its tappable example requests. Keep a
   stopwatch as a fallback.
3. Print or show the three task cards (below) one at a time. Do not show the chat
   examples or the evidence page.

## Tasks (same three on both interfaces)

| ID | Task card (what the tester reads) | Correct outcome |
|---|---|---|
| T1 | "Send $45 to Ah Ma from your savings account." | 1 transfer: Ah Ma (POSB ••7766), Savings ••1234, SGD 45.00 |
| T2 | "Pay your Singtel bill of $39.90 from your current account, and send John Tan $120 from your savings." | Singtel bill 39.90 from Current ••5678; transfer John Tan (OCBC ••8892) 120.00 from Savings ••1234 |
| T3 | "Pay off your whole credit card balance from savings. Then send Mum half of that amount from your current account." | Card ••4242 642.30 from Savings ••1234; Mum (DBS ••9988) 321.15 from Current ••5678 |

Equity purchases are not in the tasks because the baseline form has no equity flow.
The baseline handles one payment at a time, so T2 and T3 take two passes there; that
difference is part of what is being compared.

## Order

Alternate the interface order to spread learning effects: P1, P3, P5 use the baseline first
(order **A**); P2, P4 use DCTA first (order **B**). Tasks always run T1 → T2 → T3 within an
interface. Give a short break between interfaces.

## Measures (per tester × interface × task)

| Measure | How |
|---|---|
| Completion time (s) | From reading the card to the receipt/"payment recorded" screen (facilitator timer or stopwatch) |
| Interactions | Facilitator timer's control-activation count; for chat also note the number of messages sent |
| Success | All legs have the correct recipient, source and amount, and the tester approved |
| Incorrect approval | The tester approved something that differs from the task card (any leg) |
| Comprehension before approval | Just before the tester approves, ask: "Who will be paid, from which account, and what is the total amount leaving your accounts?" Record recipient / source / total as correct Y/N each. Do not correct them |
| Clarifications seen (chat) | Number of app questions shown |
| Assistance | Any help given by the facilitator (Y/N + what) |

Record observations separately from interpretation: what the tester did or said (quote only
with permission), then any hypothesis, labelled as such.

## After the sessions

1. Fill `results_template.csv` (one row per tester × interface × task).
2. Report medians and ranges with n, per interface and task; list every incorrect
   approval and every comprehension miss individually.
3. Pick **at least one observation** that suggests a UI change. Make the change, rerun the
   affected checks (web build, the relevant pytest/browser walkthrough), and record
   "observation → change → recheck result" in `docs/evidence/T-14-usability.md`.
4. Label all findings exploratory (n≈5, one demo user, synthetic data, facilitator present,
   learning effects only partly balanced).
