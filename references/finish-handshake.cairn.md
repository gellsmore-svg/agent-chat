# Agent Chat finish handshake

Process contract for a two-participant protocol v2 discussion. The Agent Chat journal records what was proposed, accepted, confirmed, or left open. Apply the map before each turn and stop at a recorded terminal outcome.

## Transition rules

| Latest committed turn | New intent | Result |
| --- | --- | --- |
| Any nonterminal state | `continue` | Discussion advances. A previous finish proposal ceases to be eligible for acceptance. |
| Any nonterminal state | `propose_finish` | The new turn is the sole proposal eligible for acceptance. If it replaces a proposal, state what changed. |
| Counterpart's `propose_finish` | `accept_finish` | Accept the immediately preceding proposal as written and start a finish check. |
| An accepted proposal with zero confirmations | `confirm_finish` from the proposer | Record that the proposer found no outstanding work. Wait for the counterpart's confirmation. |
| An accepted proposal with one confirmation | `confirm_finish` from the counterpart | Record that the counterpart found no outstanding work. Complete by agreement and identify all four closing turns. |
| Any other latest turn, or an empty journal | `accept_finish` | Reject the transition before commit. Apply the runner's bounded response retry. |
| No accepted proposal awaiting a confirmation | `confirm_finish` | Reject the transition before commit. Apply the runner's bounded response retry. |
| Deadline, turn limit, cancellation, or failure ends the session before the second valid confirmation commits | Any | Report the recorded outcome and any unresolved proposal or questions. |

The runner derives the current proposal and its confirmations from consecutive journal turns. Whether the reply agrees to the terms and whether work remains are participant responsibilities. Accept unchanged terms directly unless the objective explicitly calls for a fresh proposal. A qualification that changes the terms requires a counterproposal. A clarification request or outstanding work uses `continue` and clears the current finish check. Finish turns require empty `open_questions` and `actions` arrays.

```cairn
REQUIREMENTS
  R1. A participant accepts only the immediately preceding proposal from the counterpart. [MUST]
      ACCEPTANCE: Given propose_finish followed by accept_finish, the runner keeps the chat open and requests a confirmation from the proposer.
  R2. Changed terms are a counterproposal and a clarification keeps discussion open. [MUST]
      ACCEPTANCE: A counterproposal or continue resets both confirmations. A stale confirmation is invalid.
  R3. Both participants confirm the accepted terms and the absence of outstanding work. [MUST]
      ACCEPTANCE: Completion requires both confirm_finish turns with no recorded open questions or actions. A deadline, turn limit, cancellation, or failure before the second confirmation preserves its own outcome.
  R4. Readers can see which turn advanced, proposed, counterproposed, or accepted. [SHOULD]
      ACCEPTANCE: The transcript shows each recorded intent and identifies the proposal, acceptance and confirmation turns when complete.

PROCESS FinishHandshakeTurn (INPUT: session config, committed turns; OUTPUT: committed turn or terminal outcome)
  STATE
    latest_turn [scope: session; dir: read]
    outcome [scope: session; dir: read/write]
  1. <identify-proposal> Derive the active proposal from the latest committed turn. [CODE, DETERMINISTIC] [SATISFIES: R1, R2]
     OUTPUT: no active proposal or its turn number, terms and confirmation count
  2. <frame-response> Give the next participant the eligible proposal and allowed intents. [CODE, DETERMINISTIC] [SATISFIES: R1, R2]
  3. <choose-intent> Respond to the active proposal or advance the discussion. [LLM, STOCHASTIC] [SATISFIES: R1, R2]
     CONSTRAINTS: Accept unchanged terms unless the objective asks for a fresh proposal. Confirm an accepted proposal only after checking that nothing remains outstanding. Continue to record a blocker or ask for clarification.
     OUTPUT: one structured intent and its supporting message
  4. <commit-turn> Validate the candidate against the latest turn and session bounds before committing. [CODE, DETERMINISTIC] [SATISFIES: R1, R2, R3]
     CONSTRAINTS: An invalid acceptance or confirmation cannot commit. The second distinct participant's confirmation must commit within the session bounds to record mutual completion.
  5. <publish-outcome> Project the intent and relevant turn references into the transcript. [CODE, DETERMINISTIC] [SATISFIES: R3, R4]
     OUTPUT: next turn or terminal status with accepted proposal references or unresolved matters
```
