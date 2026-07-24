import pandas as pd
import numpy as np
import os
import uuid

# File paths
SYNTHETIC_DATA_PATH = "corporate_approval_data.csv"
REAL_LOAN_DATA_PATH = "Datasets/HuggingFace Datasets/master-loan-approval-data.csv"
COMBINED_DATA_PATH = "combined_corporate_approval_data.csv"

def prepare_and_merge_data():
    print(f"Loading synthetic data from {SYNTHETIC_DATA_PATH}...")
    df_synthetic = pd.read_csv(SYNTHETIC_DATA_PATH)
    
    print(f"Loading real-world loan data from {REAL_LOAN_DATA_PATH}...")
    df_real = pd.read_csv(REAL_LOAN_DATA_PATH)
    
    print("Harmonizing real data to match corporate schema...")
    # Harmonize real data columns to match our schema:
    # Schema: Request_ID, Employee_ID, Role, Department, Request_Type, Destination, Amount_INR, Description, Is_Anomaly, AI_Risk_Score
    
    # 1. Map columns
    # We use Employment_Type as Role
    # We use Loan_Purpose as Request_Type
    # We use Loan_Amount * 80 (approx USD to INR) for Amount_INR
    
    mapped_real = pd.DataFrame()
    
    num_rows = len(df_real)
    mapped_real['Request_ID'] = ["REAL_" + str(uuid.uuid4()).split('-')[0].upper() for _ in range(num_rows)]
    mapped_real['Employee_ID'] = ["EXT_" + str(np.random.randint(1000, 9999)) for _ in range(num_rows)]
    mapped_real['Role'] = df_real['Employment_Type']
    
    # Randomly assign departments to make it look like corporate data, but maintain the distribution
    departments = ['Sales', 'Engineering', 'Marketing', 'HR', 'Data Science']
    mapped_real['Department'] = np.random.choice(departments, num_rows)
    
    mapped_real['Request_Type'] = df_real['Loan_Purpose']
    mapped_real['Destination'] = 'External Validation'
    mapped_real['Amount_INR'] = df_real['Loan_Amount'] * 80  
    mapped_real['Description'] = "Real-world historical request"
    
    # Map anomalies. Let's define an anomaly based on extreme Debt_to_Income_Ratio or very low Credit_Score
    # which resulted in a Denial. This provides a real-world pattern of anomalous requests.
    is_anomaly = ((df_real['Debt_to_Income_Ratio'] > 0.6) | (df_real['Credit_Score'] < 400)) & (df_real['Loan_Status'] == 'Denied')
    mapped_real['Is_Anomaly'] = is_anomaly.astype(int)
    
    # AI_Risk_Score approximation based on Debt_to_Income and Credit_Score
    risk_score = (df_real['Debt_to_Income_Ratio'] * 50) + ((850 - df_real['Credit_Score']) / 10)
    mapped_real['AI_Risk_Score'] = np.clip(risk_score, 0, 100).round(2)
    
    print("Sample of harmonized real data:")
    print(mapped_real.head())
    
    print(f"\nCombining datasets... (Synthetic: {len(df_synthetic)}, Real: {len(mapped_real)})")
    # Concatenate both datasets
    df_combined = pd.concat([df_synthetic, mapped_real], ignore_index=True)
    
    # Shuffle the combined dataset
    df_combined = df_combined.sample(frac=1, random_state=42).reset_index(drop=True)
    
    print(f"Saving combined dataset to {COMBINED_DATA_PATH}...")
    df_combined.to_csv(COMBINED_DATA_PATH, index=False)
    
    print("\nData Preprocessing and Integration (Phase 1) is complete!")
    print(f"Total records in combined dataset: {len(df_combined)}")

if __name__ == "__main__":
    prepare_and_merge_data()
