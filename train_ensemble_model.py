import os
import sys

import joblib
import numpy as np

import model_pipeline

DATA_PATH = "combined_corporate_approval_data.csv"
MODEL_PATH = "ensemble_ai_model.pkl"


def save_roc_curve(labels, scores):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from sklearn.metrics import roc_auc_score, roc_curve
    except ImportError:
        print("matplotlib is not installed; skipping roc_auc_curve.png")
        return

    fpr, tpr, _ = roc_curve(labels, scores)
    plt.figure(figsize=(8, 6))
    plt.plot(fpr, tpr, color="blue", label=f"ROC Curve (AUC = {roc_auc_score(labels, scores):.2f})")
    plt.plot([0, 1], [0, 1], color="red", linestyle="--")
    plt.xlabel("False Positive Rate")
    plt.ylabel("True Positive Rate")
    plt.title("ROC-AUC Curve for Approval Classifier")
    plt.legend(loc="lower right")
    plt.savefig("roc_auc_curve.png")
    print("ROC-AUC curve saved as roc_auc_curve.png")


def main(data_path=DATA_PATH, model_path=MODEL_PATH):
    if not os.path.exists(data_path):
        print(f"Error: Could not find {data_path}")
        return 1

    print(f"Loading data from {data_path}...")
    base = model_pipeline.base_training_data_from_csv(data_path)

    print("Training XGBoost, Isolation Forest, One-Class SVM and SHAP explainer...")
    artifacts, holdout = model_pipeline.train_ensemble(base)

    metrics = artifacts["metrics"]
    print(f"\nTraining rows: {metrics['training_rows']} | Holdout rows: {metrics['holdout_rows']}")
    print(f"ROC-AUC:  {metrics['roc_auc']:.4f}")
    print(f"Accuracy: {metrics['accuracy']:.4f}")
    print(f"F1-Score: {metrics['f1']:.4f}")

    scores = artifacts["xgboost_model"].predict_proba(model_pipeline.encode_features(artifacts, holdout))[:, 1]
    total = len(scores)
    print("\n--- Confidence-Based Triage on Holdout ---")
    print(f"Auto-Approved:              {np.sum(scores > 0.8) / total:.1%}")
    print(f"Escalated (low confidence): {np.sum(scores < 0.2) / total:.1%}")
    print(f"Manual Review:              {np.sum((scores >= 0.2) & (scores <= 0.8)) / total:.1%}")

    print("\nForm options offered to employees:")
    for field, values in artifacts["form_options"].items():
        print(f"  {field}: {', '.join(values)}")

    save_roc_curve(holdout["label"].to_numpy(), scores)
    joblib.dump(artifacts, model_path)
    print(f"\nSaved ensemble pipeline to {model_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
