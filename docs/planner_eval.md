# Planner Isolation Evaluation (PRD FR-1.2)

## Evaluation Rubric
- **Specificity (1–5)**: 5 = Names specific entities, technologies, metrics, or timeframes. 3 = Somewhat generic. 1 = Vague.
- **Independence (1–5)**: 5 = Fully self-contained; answering does not require prior sub-question context. 1 = Tightly coupled.

## Results Across 10 Questions

### Question 1: *"Compare the main approaches to retrieval-augmented generation evaluation and what each measures."*

| ID | Sub-Question | Specificity (1-5) | Independence (1-5) | Notes |
|---|---|---|---|---|
| `sq_1` | What are the primary component metrics used in RAG evaluation frameworks such as RAGAS (faithfulness, answer relevancy, context precision)? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |
| `sq_2` | How do reference-based evaluation benchmarks (e.g., RGB, CRUD) compare with reference-free LLM-as-a-judge approaches in RAG? | 3/5 | 5/5 | Acceptable. |
| `sq_3` | What are the documented limitations and biases of LLM-as-a-judge methodologies when grading RAG outputs? | 3/5 | 5/5 | Acceptable. |

### Question 2: *"What changed in the EU AI Act's general-purpose model obligations over the last year?"*

| ID | Sub-Question | Specificity (1-5) | Independence (1-5) | Notes |
|---|---|---|---|---|
| `sq_1` | What specific criteria define 'systemic risk' general-purpose AI models under the EU AI Act adopted in 2024? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |
| `sq_2` | What copyright compliance and training data transparency mandates apply to all GPAI providers under the finalized Act? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |
| `sq_3` | What is the enforcement timeline and role of the European AI Office for GPAI compliance through 2025-2026? | 3/5 | 5/5 | Acceptable. |

### Question 3: *"How do modern speculative decoding techniques achieve inference speedups in large language models?"*

| ID | Sub-Question | Specificity (1-5) | Independence (1-5) | Notes |
|---|---|---|---|---|
| `sq_1` | How does draft-model speculative decoding differ from self-speculative techniques (e.g., Medusa, Lookahead) in LLM inference? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |
| `sq_2` | What mathematical acceptance criteria (e.g., speculative sampling) are used to guarantee output distribution invariance? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |
| `sq_3` | What are the reported latency speedups and memory bandwidth trade-offs across batch sizes for speculative decoding? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |

### Question 4: *"What are the trade-offs between Redis AOF everysec vs always in terms of throughput and data loss?"*

| ID | Sub-Question | Specificity (1-5) | Independence (1-5) | Notes |
|---|---|---|---|---|
| `sq_1` | What is the exact fsync behavior and Linux system-call frequency of Redis AOF configured with appendfsync everysec? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |
| `sq_2` | How does write throughput and IOPS differ between appendfsync always and appendfsync everysec under benchmark workloads? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |
| `sq_3` | Under what crash conditions can Redis AOF everysec lose more than 1 second of committed data? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |

### Question 5: *"Analyze the architectural differences between LangGraph and Autogen for stateful workflows."*

| ID | Sub-Question | Specificity (1-5) | Independence (1-5) | Notes |
|---|---|---|---|---|
| `sq_1` | How does LangGraph's cyclic state-graph model differ from AutoGen's conversational multi-agent paradigm in state persistence? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |
| `sq_2` | What checkpointing, time-travel, and human-in-the-loop debugging capabilities exist natively in LangGraph vs AutoGen? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |
| `sq_3` | How do both frameworks handle tool failure recovery, retry loops, and deterministic budget limits in execution graphs? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |

### Question 6: *"What are the proven environmental impacts of datacenter water cooling in dry climates?"*

| ID | Sub-Question | Specificity (1-5) | Independence (1-5) | Notes |
|---|---|---|---|---|
| `sq_1` | What are the documented gallons-per-MWh water consumption metrics of evaporative cooling systems in arid-region datacenters? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |
| `sq_2` | What stress does hyperscale datacenter water withdrawal place on municipal aquifers and agricultural water allocations? | 3/5 | 5/5 | Acceptable. |
| `sq_3` | How do closed-loop liquid cooling and direct-to-chip cooling alternatives reduce consumptive water use in modern facilities? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |

### Question 7: *"How does the UK's copyright legal framework address generative AI training on public web data?"*

| ID | Sub-Question | Specificity (1-5) | Independence (1-5) | Notes |
|---|---|---|---|---|
| `sq_1` | How does the UK Copyright, Designs and Patents Act 1988 (CDPA) treat text and data mining (TDM) for commercial AI models? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |
| `sq_2` | What outcomes or legal precedents have emerged from ongoing UK litigation regarding unauthorized training on copyrighted works (e.g., Getty Images v. Stability AI)? | 3/5 | 5/5 | Acceptable. |
| `sq_3` | What code of practice or legislative reforms has the UK Intellectual Property Office proposed for AI copyright licensing? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |

### Question 8: *"What is the mathematical formulation of BM25Okapi and how does document length normalization work?"*

| ID | Sub-Question | Specificity (1-5) | Independence (1-5) | Notes |
|---|---|---|---|---|
| `sq_1` | What is the formal probabilistic ranking function formula for BM25Okapi, including term frequency and inverse document frequency components? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |
| `sq_2` | How do tuning parameters k1 and b specifically control term frequency saturation and document length penalization in BM25? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |
| `sq_3` | In what information retrieval scenarios does BM25Okapi fail compared to dense embedding retrieval or BM25+ | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |

### Question 9: *"Compare the latency and throughput characteristics of Groq LPUs versus Nvidia H100 GPUs for Llama-3."*

| ID | Sub-Question | Specificity (1-5) | Independence (1-5) | Notes |
|---|---|---|---|---|
| `sq_1` | What architectural features (SRAM memory, deterministic execution) differentiate Groq LPUs from Nvidia H100 GPUs for transformer inference? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |
| `sq_2` | What are the reported tokens-per-second per-user throughput and time-to-first-token benchmarks for Llama-3-70B on Groq vs H100? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |
| `sq_3` | How do concurrent multi-tenant throughput scaling and power consumption per million tokens compare between Groq racks and H100 HGX systems? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |

### Question 10: *"What are the established mitigation strategies for Server-Side Request Forgery in modern microservices?"*

| ID | Sub-Question | Specificity (1-5) | Independence (1-5) | Notes |
|---|---|---|---|---|
| `sq_1` | How do DNS rebinding attacks circumvent naive IP-based SSRF filters in web fetchers? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |
| `sq_2` | What network-level and transport-layer mitigations (e.g., custom socket resolvers, egress firewall policies) prevent SSRF in microservices? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |
| `sq_3` | How should redirect handling be implemented to ensure redirected URLs cannot bypass initial SSRF checks? | 5/5 | 5/5 | Specific entity/metric named; fully self-contained. |

## Aggregate Summary
- **Total Questions Evaluated**: 10
- **Total Sub-Questions Generated**: 30
- **Mean Specificity Score**: 4.67 / 5.0
- **Mean Independence Score**: 5.00 / 5.0
- **Pass Condition**: Mean Specificity >= 4.0 and Mean Independence >= 4.0. Status: **PASSED**

---
*Generated automatically by `scripts/eval_planner.py`.*