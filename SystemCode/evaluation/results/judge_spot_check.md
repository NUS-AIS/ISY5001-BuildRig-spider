# Spot check of the LLM judge (explanation quality)

- Checked on 2026-10-11 against the results of commit `9e36926` (`judge_items.jsonl`, `e1_records.jsonl`).
- **Reviewer: Claude (an AI assistant), not a person.** This is an independent second opinion from a different model
  than the one that wrote and graded the explanations (qwen3:8b did both). It does not replace a human check.
- Sample: 10 of the 33 judged explanations, drawn with `random.seed(20261011)`.
- Method: each reason and trade-off was compared with the option's listed parts, extracted specs, prices and
  validation checks in `e1_records.jsonl`, using the judge's own rubric (grounded / relevant / honest, 1-5, strict).

## Result

| | Grounded | Relevant | Honest | Overall |
|---|---|---|---|---|
| LLM judge (all 33 items) | 5.0 | 5.0 | 5.0 | 5.0 |
| LLM judge (these 10) | 5.0 | 5.0 | 5.0 | 5.0 |
| This review (these 10) | 4.4 | 4.0 | 4.2 | 4.2 |

The judge gave 5/5/5 to every one of the 33 items, so it does not discriminate. In this sample 4 of 10 deserve
full marks; one contains a false statement the judge and the explanation guard both missed.

## Per item

| Case | Review (G/R/H) | Finding |
|---|---|---|
| D02 | 4 / 4 / 4 | Facts match the parts. "Strong multi-threaded performance" for an i5-14400F is generous. Says nothing about 16GB of memory and a 500GB SSD being tight for SolidWorks. |
| L01 | 5 / 5 / 5 | Every claim matches the listing (32GB, 1TB, S$1,799, integrated graphics, regional check unknown). |
| L03 | 4 / 4 / 4 | Says "Intel Core i7"; the listing is an "Intel Core 7 240H", a different product line. Does not mention the single 16GB stick. |
| L04 | 5 / 5 / 5 | Matches the listing; notes the lack of a dedicated card for GPU-based MATLAB work. |
| C01 (laptop option) | 2 / 3 / 3 | **False:** "Includes a dedicated Intel GPU". The listing says "INTEL GPU", which is integrated graphics. The trade-off "higher power consumption due to the dedicated GPU" follows from the same mistake. |
| K01 | 4 / 5 / 5 | "Competitive price" for the motherboard is not supported by anything listed. Trade-offs are good: 16GB and 500GB are named as limits. |
| F02 | 5 / 5 / 5 | Matches the listing and the 32GB requirement. |
| F03 | 5 / 2 / 3 | The user's message was "Please make it quiet". The explanation only restates specs and never mentions noise. Title and trade-off are the template fallback. |
| M01 | 5 / 3 / 4 | Accurate spec restatement, but no link to gaming; template title and trade-off. |
| M02 | 5 / 4 / 4 | Accurate. "May not include all the latest high-end components" is vague; a S$3,461 build with 16GB of memory is not flagged. |

## What follows from it

- The reported "explanation quality 5.0" should be read as "the judge accepts everything", not as a measured 5.0.
  A defensible figure from this sample is about 4.2, with grounding the strongest criterion.
- The explanation guard does not catch a "dedicated GPU" claim on a laptop with integrated graphics (C01).
- When the model's wording is dropped and the template is used (F03, M01), the explanation stops addressing the
  user's request; for F03 the stated preference (quiet) is not reflected in the text at all.
- Outside the explanation text, two builds in the sample pair a Ryzen 5 8500G with an RX 9070 XT or RTX 5060 and
  use a single 16GB stick near the top of the budget (F03, M01, M02). That is planning quality, not explanation quality.

## Follow-up (2026-10-11, after this check)

Changes made in response; the figures above still describe commit `9e36926` and were not re-measured.

- The guard now drops a claim of dedicated graphics for an option that has none, and a processor class ("Core i7")
  that no listing in the option names. Replaying all 230 explanation sentences of this evaluation through the new
  guard removes 5 (two in C01, one reason and one summary sentence in L03, one in C04) and no others.
- The template explanation in D05, F03, F04 and M01 (6 of 39 options) came from the model rewriting the option id,
  so its text was discarded. Options are now matched by position when the id does not match.
- Quiet, white and RGB are confirmed only where a listing says so. Otherwise sentences claiming them are removed and
  one trade-off states that the wish could not be checked.
- Not fixed: a sentence that credits the wrong part with a wish some other part meets ("the processor provides
  white aesthetics") still passes, and vague or generous wording is not something the guard judges.
