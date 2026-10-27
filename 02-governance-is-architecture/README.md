# 02 · Governance is architecture

> **The idea:** the AI governance that actually protects a business is rarely a policy document. It's a handful of design decisions, often a few lines of configuration, made early enough to shape everything downstream.

Companion to the LinkedIn article *"The Best AI Safety Control I've Seen Was a Missing Tool."*

## The seven decisions

![Seven design decisions that did the governance](diagram.png)

| # | Decision | The question it answers | Lesson |
|---|---|---|---|
| 1 | **The AI never calculates** | Can the model invent a number? | coming 15 Dec |
| 2 | **A catalog, not a keyboard** | Can the model write any query it likes against production data? | coming 17 Nov |
| 3 | **Weight the rules above the model** | What happens when the model gets worse? | |
| 4 | **Every score ships with its reasons** | Can the person who must act understand *why*? | |
| 5 | **Autonomy is earned, not granted** | Who decides how much the AI does on its own, and based on what evidence? | coming 26 Jan |
| 6 | **Deny capabilities, don't forbid behaviors** | If the model is tricked, what can it actually do? | coming 9 Feb |
| 7 | **Decide "good enough" before you build** | How will we know it works, and can the test lie to us? | |

Decision 6 in one picture: the agent proposes, only the gate can approve.

![The best AI safety control was a missing tool](missing-tool.png)

## Using them as a project manager

Turn each decision into a question for your next design review. If the answer is *"we'll handle that in the prompt"*, write it in the **risk log** with an owner. A prompt is a request, not a control.

A one-page checklist:

- [ ] Every number shown to users comes from SQL or a statistical model, never from the language model.
- [ ] The model reaches data through a defined catalog or API, not free-form SQL.
- [ ] Deterministic business rules stay in place. The model adds to them rather than replacing them.
- [ ] Every score or recommendation shows the reasons behind it.
- [ ] The level of autonomy is staged, measured, and can be reduced again.
- [ ] Risky capabilities (writing, approving, sending, paying) are **removed** from the AI, not just discouraged.
- [ ] Acceptance tests exist before the build, including negative cases where the AI must *not* act.

## See it working

[Argus](../argus/), the open revenue assurance app in this repo, applies several of these decisions: every figure comes from SQL and is checked after each answer (1), the AI can only name fields from a fixed list (2), fixed rules run alongside the statistics and caught a fault the statistics had already learned to accept (3), and the AI analyst has six denied capabilities, each written down with the failure it prevents (6).

## Go deeper

- [NIST AI Risk Management Framework](https://www.nist.gov/itl/ai-risk-management-framework): the reference framework for AI risk.
- [Google's Secure AI Framework (SAIF)](https://saif.google/): practical controls for securing AI systems.
- [OWASP Top 10 for LLM applications](https://genai.owasp.org/llm-top-10/): "excessive agency" is decision 6 in security language.
- [Building effective agents (Anthropic)](https://www.anthropic.com/engineering/building-effective-agents): when to use agents and how to keep them bounded.

---

📣 LinkedIn article: [The Best AI Safety Control I've Seen Was a Missing Tool](https://www.linkedin.com/pulse/best-ai-safety-control-ive-seen-missing-tool-maicon-abreu-moopf) · Previous: [01 · No API keys](../01-no-api-keys/) · Next: 03 · The data decides the technique (coming 3 Nov)
