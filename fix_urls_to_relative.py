import os

files = ['user.html', 'admin.html', 'super_admin.html', 'index.html']

for f in files:
    if os.path.exists(f):
        with open(f, 'r', encoding='utf-8') as file:
            content = file.read()
        
        # In index.html, we had: const AUTH_API = isLocal ? 'http://localhost:5001/api/auth' : 'https://your-auth-api.onrender.com/api/auth';
        content = content.replace("const AUTH_API = isLocal ? 'http://localhost:5001/api/auth' : 'https://your-auth-api.onrender.com/api/auth';", "const AUTH_API = '/api/auth';")
        content = content.replace("const isLocal = window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1';", "")
        content = content.replace("// Dynamic API URL for production vs local", "")

        # In other files, we had: (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1' ? 'http://localhost:5001' : 'https://your-auth-api.onrender.com') + '
        content = content.replace("(window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1' ? 'http://localhost:5001' : 'https://your-auth-api.onrender.com') + '", "'")
        content = content.replace("`${window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1' ? 'http://localhost:5001' : 'https://your-auth-api.onrender.com'}", "")
        content = content.replace("(window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1' ? 'http://127.0.0.1:5000' : 'https://your-ml-api.onrender.com') + '", "'")

        with open(f, 'w', encoding='utf-8') as file:
            file.write(content)
print('Relative URLs restored.')
