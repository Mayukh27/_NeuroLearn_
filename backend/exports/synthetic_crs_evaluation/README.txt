SYNTHETIC DETERMINISTIC CRS EVALUATION
These outputs use constructed inputs and are not learner or participant evidence.
Ablation sets one component weight to zero and divides each remaining weight by their sum (0.8), preserving a total weight of 1.
Sensitivity sweeps valid score histories, B percentages, timing ratios, transcript complexity examples, and six valid weight profiles.
The score-history axis affects both P and T because both use the same assessment history in the documented implementation.
Run from backend/: python scripts/crs_offline_evaluation.py --out exports/synthetic_crs_evaluation
