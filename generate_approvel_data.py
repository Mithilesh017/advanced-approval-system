import csv
import random

def generate_corporate_data(filename="corporate_approval_data.csv", rows=20000):
    print(f"Generating {rows} rows of highly accurate approval data...")
    random.seed(42) # Ensuring reproducibility
    
    # Configuration Arrays
    roles = ['Junior Developer', 'Senior Engineer', 'Product Manager', 'Director', 'Executive']
    departments = ['Engineering', 'Data Science', 'Sales', 'Marketing', 'HR']
    request_types = ['Hotel Booking', 'Flight Ticket', 'Meals & Entertainment', 'Software License']
    destinations = ['Mumbai', 'Bangalore', 'Delhi', 'Hyderabad', 'Chennai', 'Dubai', 'New York', 'London', 'Singapore']
    
    # Low-risk standard descriptions
    normal_descriptions = {
        'Hotel Booking': ['Client onsite project stay', 'Annual tech conference hotel room', 'Quarterly team sync lodging', 'Developer training accommodation'],
        'Flight Ticket': ['Onsite client deployment flight', 'Business travel to regional office', 'Tech summit roundtrip ticket', 'Executive board meeting travel'],
        'Meals & Entertainment': ['Client dinner meeting', 'Team lunch celebration', 'Project milestone dinner', 'Recruitment drive catering'],
        'Software License': ['IDE subscription renewal', 'Cloud hosting monthly allocation', 'Data analytics tool license', 'Project management platform seat']
    }
    
    # High-risk/Anomaly descriptions
    anomaly_descriptions = [
        'Luxury premium weekend resort stay', 
        'Personal vacation extension lodging', 
        'Five-star spa and relaxation suite', 
        'Family holiday travel reimbursement', 
        'Unapproved premium software upgrade'
    ]
    
    headers = [
        'Request_ID', 'Employee_ID', 'Role', 'Department', 'Request_Type', 
        'Destination', 'Amount_INR', 'Description', 'Is_Anomaly', 'AI_Risk_Score'
    ]
    
    with open(filename, mode='w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        
        for i in range(1, rows + 1):
            req_id = f"REQ_{100000 + i}"
            emp_id = f"EMP_{random.randint(1000, 9999)}"
            role = random.choices(roles, weights=[0.45, 0.35, 0.12, 0.06, 0.02])[0]
            dept = random.choice(departments)
            req_type = random.choice(request_types)
            dest = random.choice(destinations)
            
            # Determine if this row is a synthetic anomaly (Targeting ~5% rate)
            is_anomaly = 1 if random.random() < 0.05 else 0
            
            # Generate Amounts dynamically based on Role and Anomaly Status
            if is_anomaly:
                # Force an extreme or rule-breaking amount
                if role in ['Junior Developer', 'Senior Engineer']:
                    amount = random.randint(120000, 350000) # Way too high for engineering levels
                else:
                    amount = random.randint(400000, 950000)
                desc = random.choice(anomaly_descriptions)
                risk_score = round(random.uniform(75.0, 99.9), 2)
            else:
                # Normal distributions per role hierarchy
                if role == 'Junior Developer':
                    amount = random.randint(2500, 15000)
                elif role == 'Senior Engineer':
                    amount = random.randint(5000, 30000)
                elif role == 'Product Manager':
                    amount = random.randint(15000, 65000)
                elif role == 'Director':
                    amount = random.randint(40000, 150000)
                else: # Executive
                    amount = random.randint(60000, 250000)
                
                desc = random.choice(normal_descriptions[req_type])
                risk_score = round(random.uniform(5.0, 35.0), 2)
                
            writer.writerow([
                req_id, emp_id, role, dept, req_type, 
                dest, amount, desc, is_anomaly, risk_score
            ])
            
    print(f"✔ Dataset successfully created: {filename} ({rows} rows)")

if __name__ == "__main__":
    generate_corporate_data()