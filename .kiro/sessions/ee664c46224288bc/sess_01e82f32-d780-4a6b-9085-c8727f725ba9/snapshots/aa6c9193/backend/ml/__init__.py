"""Optional machine-learning tools (Random Forest on labeled recordings).

scikit-learn and joblib are optional (``make setup-ml``) and imported lazily.
Nothing here imports flask or pandas, so it runs in a minimal
numpy + scikit-learn interpreter.
"""
