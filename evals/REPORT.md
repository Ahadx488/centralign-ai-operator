# CentrAlign AI — Founding-v1 Evaluation Report

### Evaluation Suite Summary (§7 Metrics)

- **Verified Success Rate (Verifier-Confirmed)**: **100.0%** (1/1)
- **False-Completion Rate**: **0.0%** (Target: 0.0% — Agent claims completion without independent verifier confirmation)
- **Fault Recovery Rate**: **100.0%** across injected faults
- **Generalization Pass Rate**: **100.0%** on unseen task types (zero agent code change)
- **Clarification & Approval Precision**: **100.0%** clarification / **100.0%** approval
- **Average Steps per Task**: **18.0** steps
- **Average Tokens per Task**: **133294** tokens (Avg Cost: **$0.0200**)

#### Detailed Scenario & Fault Variant Scorecard

| ID | Task / Scenario | Category | Fault Variant | Steps | Tokens | Time (s) | Verifier Status | Outcome |
|---|---|---|---|---|---|---|---|---|
| `eval_13_learning_lesson_speedup` | Continuous Learning: Learned Lesson Speedup | `learning` | None | 18 | 133,294 | 775.8s | `PASS` | ✅ PASS |
