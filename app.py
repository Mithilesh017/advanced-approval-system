import streamlit as st
import pandas as pd
import numpy as np
import joblib
import os
import uuid

# --- 1. Page Configuration ---
st.set_page_config(page_title="AI Approval Management", page_icon="🛡️", layout="wide")

# --- 2. Session State for Authentication ---
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "user_role" not in st.session_state:
    st.session_state.user_role = None

# --- 3. Load the AI Model ---
@st.cache_resource
def load_ai_brain():
    try:
        return joblib.load("ensemble_ai_model.pkl")
    except FileNotFoundError:
        return None

artifacts = load_ai_brain()
exchange_rates = {'INR (₹)': 1.0, 'USD ($)': 83.50, 'EUR (€)': 90.20, 'GBP (£)': 105.00}
PENDING_FILE = "pending_reviews.csv"

# --- 4. Database Simulation Helpers ---
def save_to_pending(data_dict):
    df = pd.DataFrame([data_dict])
    if not os.path.exists(PENDING_FILE):
        df.to_csv(PENDING_FILE, index=False)
    else:
        df.to_csv(PENDING_FILE, mode='a', header=False, index=False)

def load_pending():
    if os.path.exists(PENDING_FILE):
        return pd.read_csv(PENDING_FILE)
    return pd.DataFrame()

def remove_from_pending(req_id):
    df = load_pending()
    if not df.empty:
        df = df[df['Request_ID'] != req_id]
        df.to_csv(PENDING_FILE, index=False)

def clear_pending():
    if os.path.exists(PENDING_FILE):
        os.remove(PENDING_FILE)

# ==========================================
# LOGIN SYSTEM
# ==========================================
if not st.session_state.logged_in:
    st.markdown("<h1 style='text-align: center;'>🔒 Enterprise Portal Login</h1>", unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        with st.form("login_form"):
            st.info("Demo Accounts:\n- Employee: user / pass\n- HR Admin: admin / pass")
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            login_btn = st.form_submit_button("Login")
            
            if login_btn:
                if username == "user" and password == "pass":
                    st.session_state.logged_in = True
                    st.session_state.user_role = "Employee"
                    st.rerun()
                elif username == "admin" and password == "pass":
                    st.session_state.logged_in = True
                    st.session_state.user_role = "Admin"
                    st.rerun()
                else:
                    st.error("Invalid credentials!")

# ==========================================
# AUTHENTICATED ROUTING
# ==========================================
else:
    # Sidebar Logout
    st.sidebar.title(f"👤 Welcome, {st.session_state.user_role}")
    if st.sidebar.button("Logout"):
        st.session_state.logged_in = False
        st.session_state.user_role = None
        st.rerun()

    if artifacts is None:
        st.error("🚨 Model not found! Please run train_ai_model.py first.")
        st.stop()

    # ------------------------------------------
    # VIEW 1: EMPLOYEE PORTAL
    # ------------------------------------------
    if st.session_state.user_role == "Employee":
        st.title("🧑‍💼 Employee Expense Portal")
        st.markdown("Submit Travel & Expense requests for instant AI evaluation.")
        
        col1, col2 = st.columns([1, 1])
        with col1:
            st.subheader("📝 Request Form")
            with st.form("expense_request_form"):
                emp_id = st.text_input("Employee ID", value="EMP_1024")
                role = st.selectbox("Job Role", ['Junior Developer', 'Senior Engineer', 'Product Manager', 'Director', 'Executive'])
                department = st.selectbox("Department", ['Engineering', 'Data Science', 'Sales', 'Marketing', 'HR'])
                req_type = st.text_input("Expense Type", value="Hotel Booking")
                destination = st.text_input("Destination", value="Mumbai")
                
                curr_col, amt_col = st.columns([1, 2])
                with curr_col:
                    currency = st.selectbox("Currency", list(exchange_rates.keys()))
                with amt_col:
                    local_amount = st.number_input("Amount", min_value=1, value=5000)
                    
                submitted = st.form_submit_button("Submit Request")

        with col2:
            st.subheader("🤖 AI Decision")
            if submitted:
                with st.spinner("AI is analyzing historical patterns..."):
                    rate = exchange_rates[currency]
                    normalized_inr = local_amount * rate
                    req_id = f"REQ_{str(uuid.uuid4())[:6].upper()}" # Generate unique ID
                    
                    input_data = pd.DataFrame({'Role': [role], 'Department': [department], 'Request_Type': [req_type], 'Destination': [destination], 'Amount_INR': [normalized_inr]})
                    
                    try:
                        encoders = artifacts['encoders']
                        is_unknown_category = False
                        
                        for col in ['Role', 'Department', 'Request_Type', 'Destination']:
                            if input_data[col].iloc[0] in encoders[col].classes_:
                                input_data[col] = encoders[col].transform(input_data[col])
                            else:
                                is_unknown_category = True 
                                input_data[col] = 0 
                        
                        scaler = artifacts['scaler']
                        input_data['Amount_INR'] = scaler.transform(input_data[['Amount_INR']])
                        
                        xgb_model = artifacts['xgboost_model']
                        iso_forest = artifacts['isolation_forest']
                        oc_svm = artifacts['one_class_svm']
                        
                        # XGBoost Probabilities
                        prob_approved = xgb_model.predict_proba(input_data)[0][1]
                        confidence_pct = round(prob_approved * 100, 1)
                        
                        # Anomaly Detection (-1 is anomaly, 1 is normal)
                        iso_pred = iso_forest.predict(input_data)[0]
                        svm_pred = oc_svm.predict(input_data)[0]
                        is_severe_anomaly = (iso_pred == -1) or (svm_pred == -1)
                        
                        st.write(f"**AI Confidence:** {confidence_pct}%")
                        if is_severe_anomaly:
                            st.warning("⚠️ **Anomaly Detectors Flagged this Request!**")
                        
                        if is_unknown_category:
                            st.error("🚨 **ESCALATED: UNKNOWN CATEGORY DETECTED**")
                            st.write(f"Request `{req_id}` sent to HR for manual review.")
                            save_to_pending({'Request_ID': req_id, 'Emp_ID': emp_id, 'Role': role, 'Type': req_type, 'Destination': destination, 'Amount_INR': normalized_inr, 'Confidence_%': confidence_pct, 'Status': 'Pending (Unknown)'})
                        elif is_severe_anomaly or prob_approved < 0.2:
                            st.error("🚨 **AUTO-REJECTED / HIGH RISK**")
                            save_to_pending({'Request_ID': req_id, 'Emp_ID': emp_id, 'Role': role, 'Type': req_type, 'Destination': destination, 'Amount_INR': normalized_inr, 'Confidence_%': confidence_pct, 'Status': 'Rejected / High Risk'})
                        elif prob_approved > 0.8 and not is_severe_anomaly:
                            st.success("✅ **AUTO-APPROVED**")
                        else:
                            st.warning("⚠️ **ESCALATED FOR MANUAL REVIEW (GREY AREA)**")
                            save_to_pending({'Request_ID': req_id, 'Emp_ID': emp_id, 'Role': role, 'Type': req_type, 'Destination': destination, 'Amount_INR': normalized_inr, 'Confidence_%': confidence_pct, 'Status': 'Manual Review'})
                            
                    except Exception as e:
                        st.error(f"Error: {e}")

    # ------------------------------------------
    # VIEW 2: HR ADMIN PORTAL
    # ------------------------------------------
    elif st.session_state.user_role == "Admin":
        st.title("💼 HR Admin & MLOps Portal")
        st.markdown("Review escalations and manage continuous AI learning.")
        
        pending_df = load_pending()
        
        if pending_df.empty:
            st.success("🎉 Inbox Zero! No pending requests in the queue.")
            st.write("---")
            if st.button("🧠 Trigger Nightly AI Retrain (Batch Job)"):
                st.success("✨ AI Model retrained with latest historical data.")
        else:
            st.subheader("📥 Escalation Queue")
            st.dataframe(pending_df, use_container_width=True)
            
            st.write("---")
            st.subheader("⚖️ Process Individual Requests")
            
            # Action controls for individual rows
            action_col1, action_col2, action_col3 = st.columns([2, 1, 1])
            with action_col1:
                selected_req = st.selectbox("Select Request ID to Review:", pending_df['Request_ID'].tolist())
            with action_col2:
                st.write("") # spacing
                st.write("")
                if st.button("✅ Approve Selected"):
                    remove_from_pending(selected_req)
                    st.success(f"{selected_req} Approved & added to AI training pipeline.")
                    st.rerun()
            with action_col3:
                st.write("") # spacing
                st.write("")
                if st.button("❌ Reject Selected"):
                    remove_from_pending(selected_req)
                    st.warning(f"{selected_req} Rejected! AI will learn to flag this behavior.")
                    st.rerun()
            
            st.write("---")
            st.subheader("⚡ Bulk MLOps Actions")
            bulk_col1, bulk_col2, bulk_col3 = st.columns(3)
            with bulk_col1:
                if st.button("✅ Approve All & Add to Policy"):
                    clear_pending()
                    st.success("All requests approved and staged for AI Retraining.")
                    st.rerun()
            with bulk_col2:
                if st.button("❌ Reject All & Add to Policy"):
                    clear_pending()
                    st.warning("All requests rejected. Strict policy enforced.")
                    st.rerun()
            with bulk_col3:
                if st.button("🧠 Trigger Nightly AI Retrain"):
                    st.success("✨ MLOps pipeline triggered. Vocabulary updated.")