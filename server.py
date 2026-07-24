from flask import Flask, request, jsonify, send_from_directory
import os
from flask_cors import CORS
import pandas as pd
import joblib

app = Flask(__name__)
CORS(app)

# Load the ensemble AI model artifacts into memory on startup
print("Loading ensemble model artifacts...")
try:
    artifacts = joblib.load("ensemble_ai_model.pkl")
    xgb_model = artifacts['xgboost_model']
    iso_forest = artifacts['isolation_forest']
    oc_svm = artifacts['one_class_svm']
    explainer = artifacts['shap_explainer']
    encoders = artifacts['encoders']
    scaler = artifacts['scaler']
    features = artifacts['features']
    print("Model artifacts loaded successfully.")
except Exception as e:
    print(f"Error loading model artifacts: {e}")

exchange_rates = {
    'INR': 1.0,
    'USD': 83.50,
    'EUR': 90.20,
    'GBP': 105.00,
    'SGD': 61.30 # Included SGD since it's supported in the frontend
}

@app.route('/api/predict', methods=['POST'])
def predict():
    try:
        data = request.json
        
        # Extract inputs
        role = data.get('Role')
        department = data.get('Department')
        req_type = data.get('Request_Type')
        destination = data.get('Destination')
        amount = data.get('Amount')
        currency = data.get('Currency')

        # 1. Normalize Amount to INR
        rate = exchange_rates.get(currency, 1.0)
        normalized_inr = amount * rate

        # Prepare DataFrame for preprocessing
        input_data = pd.DataFrame({
            'Role': [role],
            'Department': [department],
            'Request_Type': [req_type],
            'Destination': [destination],
            'Amount_INR': [normalized_inr]
        })

        is_unknown_category = False

        # 2. Map categorical text fields with OOV fallback
        for col in ['Role', 'Department', 'Request_Type', 'Destination']:
            if input_data[col].iloc[0] in encoders[col].classes_:
                input_data[col] = encoders[col].transform(input_data[col])
            else:
                is_unknown_category = True
                input_data[col] = 0  # Safe fallback to 0

        # 3. Scale the normalized amount
        input_data['Amount_INR'] = scaler.transform(input_data[['Amount_INR']])

        # Reorder columns to match feature order used in training
        X_input = input_data[features]

        # 4. Ensemble Execution
        # XGBoost Probabilities
        xgb_prob = float(xgb_model.predict_proba(X_input)[0][1])
        confidence_pct = round(xgb_prob * 100, 1)
        
        # Anomaly Detection
        iso_pred = int(iso_forest.predict(X_input)[0])
        svm_pred = int(oc_svm.predict(X_input)[0])
        is_severe_anomaly = (iso_pred == -1) or (svm_pred == -1)
        
        # SHAP Explainability
        shap_values = explainer.shap_values(X_input)
        shap_impact = dict(zip(features, [float(v) for v in shap_values[0]]))

        # 5. Decision Routing Logic (Confidence Based Triage)
        if is_unknown_category:
            status = "ESCALATED_UNKNOWN"
            message = "Unrecognized category detected (Out-Of-Vocabulary). Manual review required."
        elif is_severe_anomaly:
            status = "ESCALATED_ANOMALY"
            message = "Unusual data distribution detected by Anomaly Detectors. Flagged as anomaly."
        elif xgb_prob > 0.8:
            status = "APPROVED"
            message = "Auto-Approved based on high confidence."
        elif xgb_prob < 0.2:
            status = "ESCALATED_POLICY"  # Kept similar to previous structure for UI compatibility
            message = "Auto-Rejected based on low confidence. Manual review / policy enforcement required."
        else:
            status = "ESCALATED_MANUAL_REVIEW"
            message = "Marginal confidence score. Sent to HR for manual review (Grey Area)."

        # 6. Response Payload
        return jsonify({
            'status': status,
            'message': message,
            'confidence': confidence_pct,
            'normalized_inr': normalized_inr,
            'shap_explanations': shap_impact
        })

    except Exception as e:
        return jsonify({
            'error': str(e),
            'status': 'ESCALATED_SYSTEM_ERROR',
            'message': 'An internal system error occurred during AI processing.'
        }), 500

# Temporary route to serve frontend files locally
@app.route('/<path:filename>')
def serve_static(filename):
    # Security: Only allow serving specific safe extensions to prevent directory traversal / sensitive file exposure
    allowed_extensions = {'.html', '.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg', '.json'}
    ext = os.path.splitext(filename)[1].lower()
    
    if ext in allowed_extensions and os.path.exists(filename):
        return send_from_directory('.', filename)
    return "Not Found or Access Denied", 404

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

if __name__ == '__main__':
    app.run(port=5000, debug=False)