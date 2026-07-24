import pandas as pd
import numpy as np
from sklearn.preprocessing import LabelEncoder, StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.ensemble import IsolationForest
from sklearn.svm import OneClassSVM
from sklearn.metrics import (f1_score, roc_auc_score, roc_curve, precision_score, 
                             recall_score, accuracy_score, confusion_matrix, classification_report)
import xgboost as xgb
import shap
import joblib
import matplotlib.pyplot as plt

def train_ensemble_pipeline(data_path="combined_corporate_approval_data.csv", model_path="ensemble_ai_model.pkl"):
    print(f"Loading data from {data_path}...")
    try:
        df = pd.read_csv(data_path)
    except FileNotFoundError:
        print(f"Error: Could not find {data_path}")
        return

    # 1. Target Generation
    print("Generating Historical_Status target...")
    # 1 for Approved if Is_Anomaly is 0, 0 for Rejected if Is_Anomaly is 1
    df['Historical_Status'] = np.where(df['Is_Anomaly'] == 0, 1, 0)

    # 2. Preprocessing
    print("Preprocessing features...")
    categorical_cols = ['Role', 'Department', 'Request_Type', 'Destination']
    
    X = pd.DataFrame()
    
    encoders = {}
    for col in categorical_cols:
        le = LabelEncoder()
        X[col] = le.fit_transform(df[col])
        encoders[col] = le
        
    scaler = StandardScaler()
    X['Amount_INR'] = scaler.fit_transform(df[['Amount_INR']])
    
    features = categorical_cols + ['Amount_INR']
    y = df['Historical_Status']

    # --- Phase 2: Train-Test Split for Evaluation ---
    print("Splitting data for evaluation...")
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    # 3. Primary Classifier (XGBoost) - Phase 2 Tuning
    print("Training primary XGBoost Classifier with tuned parameters...")
    xgb_model = xgb.XGBClassifier(
        n_estimators=100, 
        max_depth=5, 
        learning_rate=0.1, 
        subsample=0.8,              # Added subsample as requested
        colsample_bytree=0.8,       # max_features equivalent for XGBoost
        n_jobs=-1,                  # Utilize all cores
        random_state=42
    )
    xgb_model.fit(X_train, y_train)

    # --- Phase 2: Evaluation Metrics ---
    print("\n--- Evaluating XGBoost Classifier ---")
    y_pred = xgb_model.predict(X_test)
    y_prob = xgb_model.predict_proba(X_test)[:, 1]

    print(f"Accuracy:  {accuracy_score(y_test, y_pred):.4f}")
    print(f"Precision: {precision_score(y_test, y_pred):.4f}")
    print(f"Recall:    {recall_score(y_test, y_pred):.4f}")
    print(f"F1-Score:  {f1_score(y_test, y_pred):.4f}")
    print(f"ROC-AUC:   {roc_auc_score(y_test, y_prob):.4f}")
    
    print("\nConfusion Matrix:")
    print(confusion_matrix(y_test, y_pred))
    
    print("\nClassification Report:")
    print(classification_report(y_test, y_pred))

    # Plot and save ROC-AUC Curve
    fpr, tpr, thresholds = roc_curve(y_test, y_prob)
    plt.figure(figsize=(8, 6))
    plt.plot(fpr, tpr, color='blue', label=f'ROC Curve (AUC = {roc_auc_score(y_test, y_prob):.2f})')
    plt.plot([0, 1], [0, 1], color='red', linestyle='--')
    plt.xlabel('False Positive Rate')
    plt.ylabel('True Positive Rate')
    plt.title('ROC-AUC Curve for Approval Classifier')
    plt.legend(loc="lower right")
    plt.savefig('roc_auc_curve.png')
    print("ROC-AUC Curve saved as 'roc_auc_curve.png'\n")

    # --- Phase 3: Confidence-Based Triage Simulation ---
    print("\n--- Simulating Confidence-Based Triage ---")
    # Thresholds: > 0.8 Auto-Approve, < 0.2 Auto-Reject, else Manual Review
    auto_approve = sum(y_prob > 0.8)
    auto_reject = sum(y_prob < 0.2)
    manual_review = sum((y_prob >= 0.2) & (y_prob <= 0.8))
    total_preds = len(y_prob)
    print(f"Total Requests Evaluated: {total_preds}")
    print(f"Auto-Approved: {auto_approve} ({auto_approve/total_preds*100:.1f}%)")
    print(f"Auto-Rejected: {auto_reject} ({auto_reject/total_preds*100:.1f}%)")
    print(f"Manual Review Needed: {manual_review} ({manual_review/total_preds*100:.1f}%)\n")

    # 4. Anomaly Safety Net (Isolation Forest) - Phase 2 Tuning
    print("Training Isolation Forest safety net with tuned parameters...")
    iso_forest = IsolationForest(
        n_estimators=100, 
        contamination=0.05, 
        max_features=1.0,           # Added max_features parameter
        bootstrap=True,             # Added bootstrap parameter
        n_jobs=-1,                  # Added n_jobs parameter
        random_state=42
    )
    iso_forest.fit(X_train)

    # 4b. Anomaly Detection (One-Class SVM) - Requested by Tech Team
    print("Training One-Class SVM anomaly detector...")
    oc_svm = OneClassSVM(
        nu=0.05, 
        kernel="rbf", 
        gamma="scale"
    )
    # Fit OneClassSVM on X_train (Note: can take time for very large datasets)
    oc_svm.fit(X_train)

    # 5. Explainability (SHAP)
    print("Initializing SHAP TreeExplainer...")
    explainer = shap.TreeExplainer(xgb_model)

    # 6. Artifact Packaging
    print(f"Packaging ensemble models and transformers to {model_path}...")
    artifacts = {
        'xgboost_model': xgb_model,
        'isolation_forest': iso_forest,
        'one_class_svm': oc_svm,
        'shap_explainer': explainer,
        'encoders': encoders,
        'scaler': scaler,
        'features': features
    }
    
    joblib.dump(artifacts, model_path)
    print("Successfully generated and saved ensemble pipeline!")

if __name__ == "__main__":
    train_ensemble_pipeline()
