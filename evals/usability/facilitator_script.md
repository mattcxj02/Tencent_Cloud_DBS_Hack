# Facilitator script (≈20 minutes per tester)

Read the parts in quotes aloud. Keep everything else neutral; do not hint at answers.

## 1. Welcome and consent (2 min)

"Thanks for helping. We're comparing two prototype ways of making payments in a banking
app. We're testing the apps, not you; there are no wrong answers.

Everything you'll see is fake: a made-up customer with made-up accounts. No real money
moves and please don't type or say any of your own bank details.

I'll time each task and count taps. I'll take written notes under a code like P3, not your
name. No screen or audio recording unless you choose to use voice input, and I'll ask you
first if you do. Notes are deleted by 15 November 2026, and you can stop or withdraw at
any time, even afterwards, without giving a reason.

Are you happy to take part?" → record **consent Y/N**. If no, stop here.

"May I quote things you say, without your name, in our write-up?" → record
**quote permission Y/N**.

## 2. Setup (1 min)

- Order for this participant: A (baseline first) for P1/P3/P5, B (DCTA first) for P2/P4.
- Fresh database (`make reset-db`), demo passkey signed in, facilitator timer on (`http://localhost:5173/?facilitator=1#/chat`; the baseline is `#/baseline`).

## 3. Tasks (≈6 min per interface)

For each interface, for T1, T2, T3:

1. Show the task card. "Please do this the way you normally would. Think aloud if you like."
2. Start the timer when they start reading.
3. Don't help unless they're stuck for about 60 seconds; if you help, note what you said.
4. Right before they approve (passkey/confirm), ask: **"Who will be paid, from which
   account, and what is the total amount leaving your accounts?"** Note each part correct
   Y/N. Do not correct them.
5. Stop the timer at the receipt / "payment recorded" screen. Note success, incorrect
   approval, interactions and (chat) number of app questions.

Short break, then the other interface.

## 4. Wrap-up (2 min)

"Which one would you rather use for payments like these, and why?" (note answer;
quote only with permission)

"Anything that confused you or that you didn't trust?"

"Thanks. Your notes stay under your code and are deleted by 15 November 2026."

## Afterwards

Enter the rows in `results_template.csv` the same day. Write observations ("did/said")
separately from hypotheses ("maybe because"). Never fill in a value you did not observe;
leave it blank and say why in `notes`.
