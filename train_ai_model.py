import pandas as pd
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler, LabelEncoder
import joblib

def train_anomaly_model(data_path="corporate_approval_data.csv", model_path="ai_approval_model.pkl"):
    print("Loading historical corporate data...")
    try:
        df = pd.read_csv(data_path)
    except FileNotFoundError:
        print(f"Error: Could not find {data_path}. Please run generate_approval_data.py first.")
        return

    print(f"Data loaded successfully: {len(df)} rows.")

    # --- Currency normalization (creates df['Normalized_Amount_INR'] without changing existing pipeline) ---
    # 2. You define your conversion rates (Base Currency = INR)
    exchange_rates = {
        'INR': 1.0,
        'USD': 83.50, # 1 USD = 83.50 INR
        'EUR': 90.20,
        'GBP': 105.00,
        'SGD': 61.30
    }

    # 3. Create a fallback mechanism (just like we did for Unknown Roles!)
    def convert_to_base(row):
        currency = row['Currency']
        local_amount = row['Local_Amount']

        # If we find the currency, multiply it. If it's a completely new currency
        # we've never seen, we default to a safe 1.0 or flag it.
        rate = exchange_rates.get(currency, 1.0)
        return local_amount * rate

    # 4. Apply the conversion to create the "AI-Ready" column
    if {'Currency', 'Local_Amount'}.issubset(df.columns):
        df['Normalized_Amount_INR'] = df.apply(convert_to_base, axis=1)
    elif 'Amount_INR' in df.columns:
        df['Normalized_Amount_INR'] = df['Amount_INR']
    else:
        df['Normalized_Amount_INR'] = np.nan

    # 1. Feature Engineering (Selecting the columns the AI will learn from)
    # We ignore Request_ID, Employee_ID, and Description for this baseline numerical model
    features = ['Role', 'Department', 'Request_Type', 'Destination', 'Normalized_Amount_INR']
    X = df[features].copy()

    print("Preprocessing data and encoding categories...")
    # 2. Convert text categories into numbers (Label Encoding)
    # AI models only understand math, so "Junior Developer" becomes 0, "Manager" becomes 1, etc.
    encoders = {}
    for col in ['Role', 'Department', 'Request_Type', 'Destination']:
        le = LabelEncoder()
        X[col] = le.fit_transform(X[col])
        encoders[col] = le # Save encoders so we can translate new dashboard inputs later

    # 3. Scale the Amount (Standardization)
    # This prevents massive numbers (like INR 5,000,000) from dominating the smaller category numbers
    scaler = StandardScaler()
    X['Normalized_Amount_INR'] = scaler.fit_transform(X[['Normalized_Amount_INR']])

    print("Training the Isolation Forest Anomaly Detection Model...")
    # 4. Train the Model
    # contamination=0.05 tells the model we expect roughly 5% of the data to be anomalous (based on our generator)
    model = IsolationForest(n_estimators=100, contamination=0.05, random_state=42)
    model.fit(X)

    # 5. Evaluate how well it learned our injected anomalies
    # The model predicts -1 for Anomalies and 1 for Normal
    predictions = model.predict(X)
    df['AI_Prediction'] = np.where(predictions == -1, 1, 0) # Map to 1=Anomaly, 0=Normal
    
    # Calculate basic accuracy against our synthetic 'Is_Anomaly' flag
    matches = (df['Is_Anomaly'] == df['AI_Prediction']).sum()
    accuracy = (matches / len(df)) * 100
    print(f"✔ Training Complete! Model Baseline Accuracy vs Synthetic Flags: {accuracy:.2f}%")

    # 6. Save the AI Brain (Pickle the model and the encoders)
    print("Saving the trained AI model to disk...")
    artifacts = {
        'model': model,
        'encoders': encoders,
        'scaler': scaler,
        'features': features
    }
    joblib.dump(artifacts, model_path)
    print(f"Success! AI Model saved as '{model_path}'. It is ready to be plugged into the dashboard.")

if __name__ == "__main__":
    train_anomaly_model()